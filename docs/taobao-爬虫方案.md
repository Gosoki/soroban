# 淘宝订单爬虫 · 方案研究（仅讨论，未动现有代码）

> 目标：把**自己淘宝账号的「已买到的宝贝」订单**自动拉进 soroban 的 `TaobaoStaging`（全部订单页），
> 供人工「导入」进账本。soroban 的暂存表 / `source` 字段 / 一单多物 / 导入工作流 / 「标签随数据自增」
> **当初就是为爬虫预留的**——这份文档只解决「怎么把订单弄进暂存表」。

日期：2026-07（July 2026）。淘宝反爬变化快，文中接口名/细节以「当时抓包为准」，不写死。

---

## 一、结论先行（推荐方案）

**Playwright 打开淘宝 H5 网页版「我的订单」→ 扫码登录一次并持久化会话 → 拦截页面里的 `mtop` XHR 拿干净 JSON → 规整成暂存行 → 写进 soroban。每个淘宝账号一份会话，定时低频跑。**

```
[Playwright 有头浏览器]                     [无头/有头复用]
  首次: 打开 login → 扫码 → 存 storage_state ──► 之后: 载入 storage_state
                                                     │
                                          打开 h5 订单页(网页版)
                                                     │
                                    page.on('response') 拦截 mtop 订单列表 JSON
                                                     │
                                    规整字段 + 映射状态枚举 → 暂存行
                                                     │
                          POST/PATCH soroban /api/staging（或直写 soroban.db）
```

**为什么是这套（而不是别的）：**
- **走 H5 网页版、不走 APP**：淘宝 APP 用 SPDY/私有协议，普通抓包代理拿不到；H5 网页版用标准 HTTPS 的 `acs.m.taobao.com/h5/mtop.*`，Playwright 能直接 `response` 拦截。**省掉 mitmproxy**。
- **让浏览器去做登录+签名**：`mtop` 请求要 `sign`（`_m_h5_tk` 令牌参与的 MD5）。浏览器自己会算，**我们不用逆向 sign 算法**，淘宝改签名也不怕。
- **抓 JSON 不抓 DOM**：拿接口返回的结构化数据，抗页面改版；DOM 解析一改版就废。
- **`storage_state` 持久化**：扫码登一次、存 cookies+localStorage，之后复用几天/几周；**少登录 = 少触发滑块/风控**（Playwright 官方与实践都印证这点，登录态复用还快 5~10×）。
- **跟你现有栈同款**：`mercari_manage` 已经在用 Playwright+抓包捞 Mercari 接口 JSON；soroban 已备好暂存表。**同一套路、复用经验**。

---

## 二、方案对比

| 方案 | 做法 | 优点 | 缺点 | 取舍 |
|---|---|---|---|---|
| **A. 浏览器抓包（推荐）** | Playwright 开 H5 订单页，拦截 mtop XHR JSON | 浏览器代劳登录+签名+风控可人工解；拿干净 JSON；抗改版 | 要跑浏览器；会话会过期需重扫码 | ✅ 首选 |
| B. mtop 直连（requests+自算 sign） | 从浏览器拿到 cookies 后，直接调 mtop 接口、自己算 sign | 轻、快、无浏览器 | 脆（签名/风控/接口名一变就挂）；cookie/令牌要不断刷；封号风险高 | 🔶 **后期优化**：先用 A 跑通，量大了再退化成直连提速 |
| C. Selenium 解析 DOM | 无头浏览器 + BeautifulSoup 解析订单页 HTML | 直观 | 一改版就废；慢；数据脏 | ❌ 不推荐 |
| D. 淘宝官方 API（TOP/开放平台） | `taobao.trades.sold.get` 等 | 合规、稳 | **买家自己的订单没有开放 API**（卖家授权类才有）；个人拿不到 | ❌ 场景不匹配，不可行 |
| E. DecryptLogin 等登录库 | requests 模拟登录 | 现成登录 | 只解决登录、不拉订单；requests 会话仍撞风控；淘宝模块靠扫码、已知有安全验证失败 issue | 🔶 只作**登录流程参考** |

---

## 三、现成轮子（已核实，别踩空）

**核心结论：GitHub 上没有维护良好的「买家订单」现成爬虫，得自己搭。** 已逐个核实：

- **[CharlesPikachu/DecryptLogin](https://github.com/CharlesPikachu/DecryptLogin)**（2.9k★）：多站模拟登录库，淘宝**只支持扫码**（`lg.taobao('', '', 'scanqr')`）。**只给登录后的 session、不拉订单**；requests 会话调订单接口仍会撞风控（[issue #74 安全验证失败](https://github.com/CharlesPikachu/DecryptLogin/issues/74)）。→ 当**扫码登录流程的参考**，不是整套方案。
- **[xzh0723/Taobao](https://github.com/xzh0723/Taobao)**：破解 H5 `sign` 参数 + **pyppeteer 登录绕过自动化检测**。**抓的是商品搜索、不是订单**，仅供学习。→ 当**「sign 算法」和「反自动化检测」的技术参考**（走方案 B 时有用）。
- **[petronny/python_unofficial_taobao_api](https://github.com/petronny/python_unofficial_taobao_api)**：Selenium，但**是卖家店铺后台**（未发货/退款订单），**不是买家「已买到的宝贝」**，且不活跃（3★）。→ 不匹配。
- 其余（[MarketSpider](https://github.com/zhangjiancong/MarketSpider)、[taobao-crawler-selenium](https://github.com/YoungZM339/taobao-crawler-selenium)、[taobao_mcp](https://github.com/JeremyDong22/taobao_mcp) 等）：**全是商品爬虫**，与订单无关。

**sign 算法（走方案 B 才需要，参考）**：`sign = md5(token + '&' + timestamp + '&' + appKey + '&' + data)`，`token` 取 `_m_h5_tk` cookie 里第一个 `_` 前的串，H5 `appKey` 常见 `12574478`。见 [cv-cat/TaoBaoApis](https://github.com/cv-cat/TaoBaoApis/blob/master/taobao_apis.py)、[H5 sign 算法说明](https://programmer.help/blogs/taobao-h5-sign-encryption-algorithm.html)。**首选方案 A 不用碰它。**

**Playwright 会话持久化**：`context.storage_state(path=...)` 存、`browser.new_context(storage_state=...)` 载。见 [Playwright Auth 文档](https://playwright.net.cn/python/docs/auth)。

---

## 四、订单接口怎么找（别写死接口名）

淘宝的买家订单 mtop 接口名会变、公开文档也查不全（搜 `queryOrderList` 找不到权威文档）。**正确做法是抓包发现**：
1. 有头浏览器登录后，打开网页版「我的订单」H5：`https://h5.m.taobao.com/mlapp/olist.html`（或从 `我的淘宝 → 已买到的宝贝` 跳转到的 H5 页）。
2. 开 DevTools Network，筛 `mtop`，找那个返回**订单列表 JSON** 的请求（形如 `//acs.m.taobao.com/h5/mtop.xxx.order.xxx/x.0/?...`）。
3. 记下它的 `api` 名、`v` 版本、`data` 结构、分页参数（一般是页码/游标 + 每页数）。
4. Playwright 里 `page.on('response')` 匹配这个 `api` 名，拿它的 `.json()`。翻页靠**滚动加载**或点「下一页」，逐页收。

> 这样即使淘宝改接口名，你重抓一次改个匹配串即可，不用动逻辑。

---

## 五、字段映射 → `TaobaoStaging`

抓到的每个订单规整成一条暂存行（soroban 侧字段）：

| soroban 暂存字段 | 淘宝订单里对应 | 备注 |
|---|---|---|
| `order_no` | 订单号 / bizOrderId | **去重键**（暂存表对非空 `order_no` 有部分唯一索引，可 upsert） |
| `taobao_account` | 当前跑的账号（acctA/acctB…） | 多账号各自打标；soroban 标签会「随数据自增」 |
| `shop` | 店铺名 / sellerNick | |
| `price_cny` | 实付款 | Decimal，别用 float |
| `order_date` | 下单时间 / gmtCreate | 转 date |
| `express_no` | 运单号 | 列表里常没有，可能要**订单详情**接口补；没有就先空 |
| `purchase_status` | 交易状态 | **要映射到 soroban 枚举**（下表） |
| items[] | 宝贝标题 + 数量 + **单价** | 一单多物；soroban `StagingItem`（name/quantity/unit_price_cny/auto） |
| `raw_json` | 整条订单原始 JSON | soroban 留了 `raw_json` 字段留底，强烈建议存 |

⚠️ 列名与枚举都变过，**以代码为准**：状态列叫 `purchase_status`（不是 `order_status`），
枚举是 `PurchaseStatus`（不是 `TaobaoStatus`），映射表的唯一真身是插件里的
`taobao_scraper/normalize.py::PURCHASE_STATUS_MAP`，由 `tests/test_consistency.py` 钉住。

**状态映射**（淘宝交易状态 → soroban `PurchaseStatus`）：
`待付款/等待买家付款→待付款`、`待发货/等待卖家发货/买家已付款/待快递上门→待发货`、
`待收货/卖家已发货/等待买家确认收货→待收货`、**`交易成功/待评价→已签收`**、
`退款中/退款成功→退款`、`交易关闭/已关闭→交易关闭`。

> **「交易成功」不是 soroban 的合法状态值**，别照抄成 `交易成功→交易成功`——推过去会被
> 后端枚举校验 422 整批拒掉。soroban 的 `已签收` 指的就是**国内**快递签收那一刻，
> 也就是淘宝页面上的「交易成功」；国际段（集运中/送达）由所挂集运单表达，不进这一列。
> 认不出的原始态名一律返回 `None`（`map_purchase_status`），**不硬塞非法值**。

> soroban 侧「待付款/退款/交易关闭」本就不计入看板合计，所以爬到这些状态照存无妨。

---

## 六、反爬 / 健壮性要点

- **低频、人速**：你订单不多，一天跑几次足够；增量只看最近/状态可能变的单，别全量猛拉。
- **风控（滑块/x5sec/需要验证）**：**有头浏览器**遇到就让你手动过一次；代码检测到 punish/验证跳转就**暂停等人工**，别硬刚。
- **令牌/会话过期**：mtop 返回 `FAIL_SYS::TOKEN_EXPIRED` → 用新的 `_m_h5_tk` **重试一次**；返回 `SID 失效/需登录` → **重扫码**刷新 `storage_state`。
- **多账号**：每个账号一份 `storage_state_<账号>.json`，串行跑、各自打 `taobao_account` 标。
- **首次有头、之后可无头**：首登必须有头扫码；之后带着 state 可无头，但**保留一条「风控时切回有头人工解」的回退**。
- **稳定指纹**：用固定的 Playwright user-data-dir/context，别每次全新指纹，减少触发风控。

---

## 七、跟 soroban 怎么接

soroban 这边**已经预留好了**，爬虫是独立进程/服务，不进 soroban 的请求链路：

- **写入方式二选一**：
  - **走 API（推荐，解耦）**：爬虫用 admin 账号登录拿 JWT，`POST /api/staging` 建新单、`PATCH /api/staging/{id}` 更状态。
  - **直写 soroban.db**：soroban 的「标签随数据自增」「union」等特性**已能容忍直写库**（第二十二/二十六版就是按这个设计的）。糙但快。
- **建议给 soroban 补一个「按订单号 upsert」端点**（**这是以后 soroban 侧要做的，不是现在**）：`POST /api/staging/upsert`，按 `order_no`：不存在则插、存在且**未导入**则更字段/状态、**已导入**则只更 `order_status`（别动已进账本的）。有了它，爬虫每轮把抓到的单一股脑推过去、幂等，最省事。在没这个端点前，先「查不到就 POST、查到就 PATCH」。
- **调度**：像 soroban 的 `fx_loop` 那样起个后台循环，或用系统 cron，按账号定时跑。

---

## 八、合规提醒（要说清）

- 爬的是**你自己账号的订单**（你自己的数据、个人记账用），性质接近「导出我的数据」，比爬别人/爬商品温和得多。
- 但**技术上仍违反淘宝的自动化 ToS**：请**只爬自己账号、低频、人速、不转售不外传**。把它当「帮我把我的订单抄进账本」的自动化，别扩大。

---

## 九、PoC 代码骨架（**未测试、参考用**，不是 soroban 代码）

```python
# 需要你本人扫码 + 真实账号，无法在此环境跑通，仅示意结构
from playwright.sync_api import sync_playwright
from pathlib import Path

STATE = lambda acc: Path(f"./.state/{acc}.json")

def login(acc):                      # 首次：有头扫码，存会话
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False)
        ctx = b.new_context()
        pg = ctx.new_page()
        pg.goto("https://login.taobao.com/")
        input(f"[{acc}] 用淘宝App扫码登录，完成后回车…")   # 人工扫码
        STATE(acc).parent.mkdir(exist_ok=True)
        ctx.storage_state(path=STATE(acc))                 # 持久化
        b.close()

def fetch_orders(acc):               # 之后：载入会话、拦截 mtop 订单 JSON
    orders = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False)              # 风控可切回有头
        ctx = b.new_context(storage_state=str(STATE(acc)))
        pg = ctx.new_page()
        def on_resp(r):
            if "mtop" in r.url and "order" in r.url.lower():   # 抓包时确认真实 api 名再收紧匹配
                try: orders.append(r.json())
                except Exception: pass
        pg.on("response", on_resp)
        pg.goto("https://h5.m.taobao.com/mlapp/olist.html")
        # TODO: 检测是否跳到登录/风控页 → 若是，login(acc) 重来 / 暂停人工
        for _ in range(5):                                 # 滚动翻页
            pg.mouse.wheel(0, 3000); pg.wait_for_timeout(1500)
        b.close()
    return orders

def normalize(raw):                  # 抓包看清结构后照字段映射表填
    return {
        "order_no": ..., "taobao_account": ..., "shop": ...,
        "price_cny": ..., "order_date": ..., "order_status": map_status(...),
        "items": [{"name": ..., "quantity": ...}], "raw_json": raw,
    }

def push(rows):                      # 走 soroban API：查不到就 POST，查到就 PATCH（或等 upsert 端点）
    ...
```

---

## 十、建议里程碑

1. **抓包摸接口**（半天）：有头登一次，DevTools 看清「订单列表」mtop 的 api 名/data/分页，确认返回 JSON 里有哪些字段（尤其运单号在不在列表里）。
2. **单账号 PoC**（1~2 天）：Playwright 扫码存 state → 拦截拉一页订单 → 打印规整结果。跑通「登录复用 + 拿到干净 JSON」。
3. **规整 + 入库**（1 天）：字段映射、状态映射、去重键 `order_no`；先「POST/PATCH」推 soroban。
4. **多账号 + 调度 + 风控回退**（1~2 天）：per-account state、定时、检测风控暂停人工。
5. **（soroban 侧，另做）** 加 `/api/staging/upsert` 幂等端点，让爬虫推送最省心。

> 想动手时我可以：先陪你抓一次包把真实接口/字段定下来，再据此写 PoC；或先在 soroban 侧把 `upsert` 端点补上。**都等你发话，现在不碰你代码。**

---

*参考来源（已核实）*：
[DecryptLogin](https://github.com/CharlesPikachu/DecryptLogin) · [issue#74 风控](https://github.com/CharlesPikachu/DecryptLogin/issues/74) · [xzh0723/Taobao](https://github.com/xzh0723/Taobao) · [petronny(卖家)](https://github.com/petronny/python_unofficial_taobao_api) · [cv-cat/TaoBaoApis(sign)](https://github.com/cv-cat/TaoBaoApis) · [H5 sign 算法](https://programmer.help/blogs/taobao-h5-sign-encryption-algorithm.html) · [Playwright Auth](https://playwright.net.cn/python/docs/auth) · [淘系App抓包难点(SPDY)](https://www.cnblogs.com/Summi/p/14491808.html)
