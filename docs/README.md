# soroban 开发记录

设计决策与开发进度。对外介绍见根目录 [README](../README.md)。

> **约定**：本 `docs/` 目录存放所有开发/设计文档，以后新文档一律放这里。

## 业务背景

个人代购／集运：在淘宝下单（付人民币）→ 国内快递 → 君丰(JF)集运把多个包裹合并转运到日本（付国际运费）→ 另有杂项开销。**最终统一按日元结算**，需要双币记账和汇率折算。使用者 2 人 + 将来的抓取机器人共用一本账。

## 技术栈决策

- **前端 Vue 3 + Element Plus**（原定 React+AntD，落地时改用与 `mercari_manage` 同栈，见下）：核心是「列表 + 编辑弹窗 + 看板」，Element Plus 的 Table/Form/Dialog 对这种场景够用够好看。数据请求 Axios + REST，需要时轮询即可看到机器人的更新（无需 WebSocket）。
  - **为何改栈**：mercari_manage（卖出侧）已是 Vue3+Element Plus，直接复用其 Layout/http(JWT)/暗色主题；且 soroban(买) 与 mercari(卖) 是同一生意两半，同栈利于将来打通利润核算。
- **后端 FastAPI + SQLModel**：Python 栈，和已有的 hiyori 项目同源。将来的抓取机器人走同一套 SQLModel 写库。
- **SQLite + WAL**：个人单机足够，WAL 让「2 人 + 机器人」的低并发读写互不阻塞。升级信号：高频并发写或上公网多人 → 换 Postgres，SQLModel 层基本不动。
- **Alembic**：从第一天管迁移（骨架期暂用 `create_all`，稳定后切 Alembic）。
- **不用 WebSocket / NiceGUI**：曾评估 NiceGUI（纯 Python 全栈），但它强依赖常连 WS；用户倾向无状态 REST + 精致前端，故走 REST + Vue3（最终选型，见上「为何改栈」）。

## 数据模型

三张业务表 + User。**金额统一「5 列」**，三张表都用：

| 列 | 谁填 | 说明 |
|---|---|---|
| `price_cny` | 手填/机器人，可空 | 原始人民币 |
| `fx_rate` | 预填当日汇率，**可改**，可空 | CNY→JPY，记实际成交汇率 |
| `jpy_auto` | **派生**（不可手填） | = round(price_cny × fx_rate) |
| `jpy_override` | 手填，可空 | 实付日元 |
| `jpy_settled` | **派生**（不可手填） | override 有则用之，否则用 auto |

> auto／settled 做成派生列，避免三列手动改到打架。直接用日元付款的情况：`price_cny`/`fx_rate` 留空，只填 `jpy_override`。

### 淘宝订单 TaobaoOrder
日期、订单号（唯一，可空）、店铺、链接、分类、状态（枚举：已付/已发/已收）、`快递号`（可空，归组用，多单可共享同一个）、`快递公司`（可空）、`淘宝账号`（有 2 个号，也便于机器人区分抓哪个）、`junfeng_order_id`（外键→君丰，**可空**=已买未集运）、金额 5 列（**订单总价，已含国内快递费**）。物品明细见 `OrderItem` 子表。

### 订单行 OrderItem（一单多物）——**物品为最小单位**
`taobao_order_id`（外键→淘宝订单）、`物品名`、`数量`、`price_cny`（**单价**，元）、`auto`（bool）。一个淘宝订单可含多行。

> **「物品为最小单位」（第四十二版起，为对接其它系统而定）**：对接的最小粒度是**物品**而非订单。
> - 每张淘宝订单/暂存**必有 ≥1 物品**；建单时没给物品，则自动补 1 条（名=商品标题、单价=订单价、`auto=True`，前端**灰显**表示系统自动、可编辑覆盖；用户一编辑即转黑）。
> - **订单人民币价由物品派生** = Σ(单价 × 数量)，见各 model 的 `sync_from_items()`；订单列表 RMB **只读**，改价走物品展开面板。`jpy_auto` 仍 = round(派生 price_cny × fx_rate)。
> - 单价口径（非行总额）：数量与单价独立；因 2 位小数，`数量>1` 且总价不整除时按单价×数量回算会有分位漂移（回填时已把这类物品标 `auto` 待复核）。
> - 爬虫只知订单总价、不知每件单价 → 后端把总价折成第一件单价、标 `auto`，其余置 0 待人工拆分；`StagingItem` 结构与字段完全对齐 `OrderItem`。

### 君丰订单 JunfengOrder
日期、君丰单号（唯一，可空）、重量、国际运单号、状态（枚举：打包中/已发出/已签收）、备注、金额 5 列（`price_cny` = 国际运费）、`特殊费_日元`（int，直填，如关税/消费税，币种**恒日元**）。**本表 `jpy_auto` = round(运费×汇率) + 特殊费_日元**；`jpy_settled` 仍 override 优先。反查 `taobao_orders`。（国内快递费不在此，已折入淘宝订单总价。）

### 杂项 MiscExpense
日期、名称、分类、备注、金额 5 列。

### 共通字段（三张表）
- `source`：`manual` / `taobao_bot` / `junfeng_bot`，区分人填还是机器人抓
- `payer_id`：外键→User，付款人（多人分摊用）
- `version`：乐观锁，人和机器人同改一行时防「后写覆盖」
- `created_at` / `updated_at`
- 索引：`日期`、`快递号`

### User
登录用。用户名 + 密码哈希。登录 token 有效期 3 个月（`TOKEN_EXPIRE_DAYS=90`）。开发期用 `python -m app.seed` 建一个 admin（默认 admin/admin123），不做开放注册。

### 看板
总额 = 淘宝(商品+快递) + 君丰(运费) + 杂项 的 `jpy_settled` 相加。

## 关系设计

- 淘宝 ↔ 君丰 = **一对多**。外键只放在淘宝一侧；ORM 提供双向导航（`taobao.junfeng_order` / `junfeng.taobao_orders`）。**不要**两边都存链接（两个真相源会漂）。
- 原本是三层（淘宝 < 快递单 < 君丰），但快递费并入淘宝、快递单不单独记费 → 砍掉快递单表，`快递号` 降为淘宝订单上的字段 → 回到干净的三张表。代价：快递号分组不被数据库强制一致，个人规模可接受。

## 决策日志

- ✅ 项目命名 `soroban`（算盤），与 hiyori 同风格
- ✅ 双币结算 = 存人民币 + 记当日汇率 + 派生日元 + 可覆盖
- ✅ 登录系统要做；**token 有效期 3 个月（90 天）**（原定超长，改为 90 天）
- ✅ 付款人字段（FK→User）
- ✅ 淘宝账号字段（2 个号）
- ✅ source / version 字段，唯一约束（订单号、君丰单号），日期/快递号索引
- ✅ **退款/取消**：加 `退款/已取消` 状态，**允许金额为负**；看板只对「有效行」求和（排除已取消）
- ✅ **结算优先级**：手填 `jpy_override` **永远最高级**，覆盖一切自动值；机器人绝不碰它
- ✅ **软删除**：三张业务表加 `deleted_at`，删除只标记不物理删，所有查询过滤 `deleted_at IS NULL`。理由：账本留痕 + 多人/机器人易误删
- ✅ **看板求和**：`jpy_auto`/`jpy_settled` 落库为普通 int 列，**写入时由 `compute_money()` 用 Decimal + ROUND_HALF_UP 精确算出**（应用维护，非 DB 生成列）。SUM/GROUP BY 照常走 SQL。
  - **落地时的取舍（原计划是 SQLite 生成列，已改）**：SQLite 把 Decimal 存成 float，生成列里 `ROUND(price_cny*fx_rate)` 有浮点边界误差风险；改用 Python Decimal 精确计算更正确，也绕开 Alembic 认不全生成列的问题（原 P2）。所有写路径都过 `compute_money()`，机器人只写暂存表不写正表，故不会漂。
- ✅ **汇率记录 + 兜底**：新增 `FxRate` 表缓存每日汇率（`date` 唯一, `rate`, `fetched_at`）；录单时取当日汇率预填，**抓取失败就用最近一次成功的汇率做基准**；每条订单仍存自己的 `fx_rate` 快照
- ✅ **开发用 admin 账号**：先用 CLI/种子脚本建一个 admin，暂不做开放注册
- ✅ **配置表**：新增 key-value `Setting` 表存应用配置（含将来淘宝账号相关配置）。⚠️ 敏感凭证（cookie/session）不明文入库，用加密或 env/单独安全存储
- ✅ **前端要好用**：列表支持筛选（日期段/状态/账号/快递号/JF 单）、搜索（订单号）、分页
- ⏸️ **收入／利润**：暂不做，留口子（见 TODO）
- ⏸️ **快递单独立成表**：暂不做。若将来「一个淘宝订单拆进多个快递号/包裹」，加关联表回填即可，迁移便宜。

### 第二轮审查决策

- ✅ **金钱类型**：CNY 用 `Decimal(12,2)`，JPY 用 `int`，**全程禁用 float**
- ✅ **汇率口径 + 校验**：`fx_rate = 1 CNY = X JPY`（X≈20）；加合理性范围校验（约 5~50），防手误 20→200 导致成本 ×10
- ✅ **导入后变更告警**：暂存行导入正表后，淘宝端若再变化（尤其退款/改价），正表不自动改；需「暂存 diff → 告警」提示人回去改（否则账悄悄错）
- ✅ **乐观锁冲突 UX**：保存撞版本返回 409，前端弹「已被他人/机器人修改，请刷新」，不静默丢
- ✅ **SECRET_KEY**：强随机、进 env、绝不提交（.env 已在 .gitignore）
- ✅ **A3 价格口径**：`price_cny` = **订单总价**，不记单品价
- ✅ **C4 按月归属**：看板按月用**每条记录自身的日期**（现金制）；商品与运费可能落在不同月，接受
- ✅ **A1 君丰费用结构（定稿）**：国内快递费**折入淘宝订单总价**（不在君丰）。君丰金额 = **运费**（CNY→JPY）+ **`特殊费_日元`**（int 直填，如关税/消费税，恒日元）。`jpy_auto = round(运费×汇率) + 特殊费_日元`，`jpy_override` 整单覆盖优先。（原计划的君丰「快递费」列取消，避免与淘宝重复。）
- ✅ **A2 一单多物（定稿）**：加轻量子表 `OrderItem(物品名, 数量)`，**不含单价**；价格仍在订单级、订单号仍唯一。〔**已被第四十二版改写**：物品加单价、订单价由 Σ(单价×数量) 派生，见「数据模型 → 物品为最小单位」〕
- ✅ **gitignore 补漏**：已加 `*.db`/`*.db-wal`/`*.db-shm`、`node_modules/`、`frontend/dist/`
- ⏸️ **C3 批量操作**：暂不做

## 淘宝抓取架构（本轮定）

机器人**只写「淘宝抓取暂存表」，绝不直接写淘宝订单正表**。原因：淘宝账号里还有很多非集运的购买记录，不一定都进账本。

- 新增 `TaobaoStaging`（抓取暂存/收件箱）：订单号、物品名、数量、店铺、价格 CNY、下单日期、淘宝账号、快递号(如有)、`raw_json`(原始留底)、`status`(`待处理/已导入/已忽略`)、`imported_taobao_order_id`(FK→正表, 可空)、`scraped_at`、`updated_at`；唯一键 (订单号, 淘宝账号) 去重。
- 流程：机器人抓 → 进暂存表；人在收件箱**手动点「导入」**才生成正式淘宝订单（`source=imported`），或点「忽略」。重抓只更新暂存，**永不动正表** —— 天然保证机器人不覆盖任何人工字段。
- 正表淘宝订单只由人写入（手工录入 或 从暂存导入）；`version` 乐观锁仍用于 2 人并发编辑。

## 待定 / TODO

- [ ] **部署形态**（#7）：跑在哪？登录必须 HTTPS；淘宝抓取从日本 IP 可能被墙/需国内代理 —— 决定机器人部署位置
- [ ] **备份策略**（#8）：WAL 下 SQLite 用 `.backup`/litestream，不能裸 copy 文件
- [ ] **收入/利润**（#9）：加 `Sale` 表（售出价/平台/手续费/关联淘宝订单）；连带要定国际运费按（重量/货值/件数）分摊到单品算「到手成本」
- [ ] 君丰订单是否也走同样的「暂存-导入」流程（JF 单基本都是集运货，可能可直接抓）
- [ ] 淘宝账号是否从字符串升级为表 + 抓取凭证的安全存储方案
- [ ] 快递号分组一致性：UI 是否加「同快递号挂了不同 JF 单」的软告警
- [ ] 退款是否支持部分退款（金额级冲抵）还是仅整单取消
- [ ] **对接快递单号平台查物流**：接国家/第三方快递查询平台（如快递100 / 快递鸟 / 官方接口），按淘宝「快递号」或君丰「国际运单号」自动拉物流状态并展示（可加缓存/定时刷新）。
- [ ] **更换汇率 API**：现用 open.er-api 免费源仅约每日更新；将来换更实时/更可靠的汇率源（改 `backend/app/services/fx.py::fetch_rate`，其余持久化/兜底逻辑可复用）。
- [ ] **SECRET_KEY 硬化**（安全审查 #18）：默认 `dev-insecure-key-change-me` 现仅启动告警、不阻断；它同时用于签 JWT **和**（经 sha256）派生加密 MySQL 密码的 Fernet 密钥——故**不能自动轮换**（换了旧的加密 DSN 就解不开）。当前绑 `127.0.0.1` 尚可；**对外/局域网暴露前必须**设强随机 `SECRET_KEY`（`python -c "import secrets;print(secrets.token_hex(32))"`）。可加：绑定非环回地址且仍是默认/弱 key 时**启动硬失败**（`backend/app/main.py` 现只 `log.warning`）。

## 待自动化（Roadmap）

- 抓淘宝订单：需登录态、反爬重，现实做法是 Playwright 带持久化登录态**半自动**抓，或手动/CSV 导入。设计上把「数据导入」做成统一入口，抓取只是加一个数据源。
- 抓君丰订单：看其是否有导出/接口，通常比淘宝好搞。
- 汇率自动填充：**从 hiyori 移植抓取逻辑（直连 open.er-api.com），在 soroban 内自成一份 `services/fx.py`**。⚠️ 不要调用 hiyori 正在跑的接口——两个项目部署位置可能不同，soroban 的汇率必须能独立运行、不依赖 hiyori 是否在线。

## 进度

**首版 MVP 已落地并自测通过**（2026-07-09）：
- 后端（`backend/app/`）：models、schemas、auth(JWT)、routers(taobao/junfeng/misc/dashboard/fx/auth)、services/fx、seed、main 全部完成；后端 smoke test 25/25 通过；真实 HTTP 端到端跑通（登录/CRUD/看板/汇率），FX 实测拉到真实汇率并持久化。
- 前端（`frontend/`）：Vue3+Element Plus，登录、Layout 侧边栏、看板、淘宝/君丰/杂项三张「列表+编辑弹窗」页，MoneyFields 实时预览；`npm run build` 与 `npm run dev` 均通过。
- 运行：后端 `cd backend && uvicorn app.main:app --reload`（先 `python -m app.seed` 建号）；前端 `cd frontend && npm run dev`（代理 /api→8000）。

**未实现（预留）**：Alembic（仍用 create_all）、机器人抓取与收件箱 UI、收入/利润(Sale)、导出、i18n(ja/en)。

### 首版代码审查（多智能体对抗审查，已全部修复）
落地后跑了一轮 5 维度并行审查 + 每条发现独立对抗验证，确认并修复 7 个缺陷（回归测试覆盖）：
1. [高] `JunfengRead.taobao_orders` 经 relationship 序列化未过滤软删 → 已改为过滤后单独组装 `TaobaoBrief`。
2. [高] 重复 order_no/junfeng_no 触发 IntegrityError 冒泡成 500 → 加全局 `IntegrityError`→409 处理器。
3. [中] `price_cny` >2 位小数使入库 CNY 与派生 JPY 不一致 → schema 与 `compute_money` 双重量化(0.01/0.0001, ROUND_HALF_UP)。
4. [中] 乐观锁只在内存比对、UPDATE 无 `WHERE version` → 并发丢失更新 → 改为 DB 层原子 `guarded_bump`（`WHERE version=期望` + rowcount 检查）。
5. [中] 软删君丰订单未清理子淘宝订单的 `junfeng_order_id`（悬空外键）→ 删除时先置空未软删子订单的挂靠。
6. [低] `int(user_id)` 在 try 外，非数字 sub → 500 而非 401 → 移入 try 并 `except (JWTError, TypeError, ValueError)`。
7. [低] 前端预览 `Math.round` 负数半值方向与后端 ROUND_HALF_UP 相反 → 改为远离零取整。

自测：后端 25 项冒烟 + 8 项修复回归 全过；前端 build/dev 通过；真实 HTTP E2E 通过。

### 第二版：Notion 式内联编辑 + 全部淘宝订单（暂存）页
- **UI 全面改版**：新增可复用组件 `NotionTable.vue`（点单元格直接改、失焦/回车自动保存、派生列只读、底部「＋ 新建一行」）。淘宝/君丰/杂项三页与新暂存页统一用它。行内保存走 PATCH `{version,[字段]}`，用返回值 `Object.assign` 更新本地行（含新 version 与派生金额）。淘宝的「物品(一单多物)」与「所属君丰」在展开面板里编辑。
- **新页「全部淘宝订单」= 暂存表 `TaobaoStaging`**：承载一个淘宝号下所有订单（含不集运的），逐单点「导入」才建正式 `TaobaoOrder`（source=imported、汇率自动预填、item_name→OrderItem），或「忽略」。现支持手动新建/内联编辑；将来爬虫只写这张表。后端 `routers/staging.py`（list/create/patch/delete/ignore/import）。
- **暂存去重**：`order_no` 部分唯一索引（`WHERE order_no IS NOT NULL`）——淘宝单号全局唯一即去重键，同时允许多条空行手动录入。
- 第二轮对抗审查修复 3 个：暂存唯一索引 NULL 不去重→改部分唯一索引；暂存页 saveCell 缺 try/catch（409 静默丢失）→补齐并刷新回退；淘宝普通单元格自动保存会覆盖展开面板里未保存的物品编辑→saveCell 不覆盖 items。
- 自测：后端 staging 13/13 + 内联版本流 8/8 + 索引 4/4；前端 build 通过；start.sh 全栈 E2E（空行→填→导入）通过。

### 第三版：改成 Gotion 表格格式
参考 mercari_manage 的 Gotion（Notion 克隆）重写表格 UI：原生 `<table>` 网格 + 行号列（悬停变删除）+ 置顶列头 + 幽灵新建行（在底行任意格输入即建行）+ 无边框内嵌输入 + select 用 tag 弹层。拆成两个组件：`GotionCell.vue`（自管编辑态的单元格，仿 Gotion cell-types）+ `NotionTable.vue`（网格外壳）。soroban 保持固定列 schema（非 Gotion 的动态列），只借其视觉与交互格式。四页 addRow 改为接收幽灵行数据。
- **真机验证**：装 Playwright + Chromium 跑 headless 浏览器 E2E，11/11 通过（登录→暂存网格渲染→列头→幽灵行建行→内联编辑触发 PATCH→淘宝网格+展开→看板卡片→无 console 报错），并截图肉眼确认 Gotion 外观（行号/占位/派生金额蓝色/幽灵行）。
- **新建行置顶**：幽灵新建行从底部移到表头下方，与「最新在上」一致。

### 第四版：暂存页支持一单多物
「全部淘宝订单」暂存页结构对齐账本淘宝订单页：新增 `StagingItem` 子表（对齐 `OrderItem`，`TaobaoStaging.items` cascade all,delete-orphan），移除单字段 `item_name/quantity`。暂存 create/patch 用 items 列表（patch 给了就整体替换），**import 把全部物品复制成账本 OrderItem**。前端暂存页加物品摘要只读列 + 展开面板编辑（加/删/保存物品），与淘宝页同款。
- 自测：后端多物 7/7（create/patch替换/blank/import复制全部/list/cascade删除）；对抗审查 **0 缺陷**；headless 截图确认物品摘要 + 展开编辑器（设定集×1，A3海报×2）。
- 踩坑记录：期间发现是 `uvicorn --reload` 残留僵尸进程占着 8000 端口、握着被 rm 的旧 db inode，导致看到旧数据——彻底 kill 端口后即恢复（代码/数据本身无误）。

### 第五版：列可拖动换位/拖宽，布局存后端
列头可拖拽换位、拖右边缘改宽；**顺序+宽度存后端**（新增 `ColumnLayout` 表 per `table_name`，JSON `[{key,width}]`），所有人/每次渲染按后端布局一致。列定义仍在代码里（含类型/label/格式/slot），后端只存「顺序+宽度」，`NotionTable.buildCols` 合并（按保存顺序重排 base 列、套宽度；base 新增列附加在后、已删列忽略）。借鉴 Gotion 的列拖拽/拖宽，但持久化到后端而非动态列。
- 后端 `routers/layout.py` GET/PUT（upsert）；前端 `NotionTable` 加 `tableName` prop，各页传 taobao/junfeng/misc/staging。
- 自测：后端 6/6（空/put/persist/upsert/独立/422）；headless 8/8（后端顺序应用于渲染、拖宽变宽+PUT、reload 持久、原生 DnD 换位+PUT、0 报错）。
- 对抗审查修 2 个：初始拉取布局的竞态（拉取期间用户改动被 GET 旧值覆盖）→ 加 `dirtyDuringLoad` 标志；后端 upsert TOCTOU → 改 `INSERT ... ON CONFLICT DO UPDATE` 原子 upsert。修后 headless 8/8 复验通过。
- **操作列移到第一列**（行号之后、固定不参与拖拽）：让操作按钮始终可见、且不侵入拖拽/布局逻辑避免引 bug（用户明确不想出别的 bug，故未做成可拖动）。headless 6/6。

> 停服务教训：`lsof -ti tcp:PORT` 会连**客户端连接**(如 VS Code 连到 dev server)一起列出，按此 kill 会误杀 VS Code helper；停 dev server 要用 `lsof -ti tcp:PORT -sTCP:LISTEN` 只杀监听进程。另外新 turn 里 shell cwd 会重置到项目根，起 vite 前要显式 cd 到 frontend。

### 第六版：操作列收窄 + 君丰关联点选（单向）
- **操作列收窄**：`actionsWidth` 默认 72（只有删除的三页），暂存页 176（导入/忽略/删除）。
- **淘宝页「君丰(点选)」列**：readonly 列里放 el-select 富选择器，选项显示每个君丰单的「单号·日期·状态·运费结算」，选中即 PATCH `junfeng_order_id`（复用 saveCell，排除 items）。移除了展开面板里的君丰 select（避免重复入口）。
- **君丰页**：展开面板把关联淘宝订单改成**只读小表格**（订单号/日期/店铺/状态/结算¥）——`TaobaoBrief` schema 加了 date/shop/status 字段，`junfeng._read` 填充。
- **单向关联**：挂靠关系只在淘宝页编辑（FK 在淘宝侧），君丰页只读展示；不做双向编辑，避免两边改同一个 FK 引 bug（用户明确要求）。
- 自测：后端 5/5（enriched brief + 从淘宝侧 relink/unlink + 旧君丰清空）；headless 7/7（操作列窄、君丰选项含参数、点选触发 PATCH、单元格显示单号、君丰页只读表、列头正确、0 报错）。

### 第七版：全面审计 + 加固（6 维度多智能体审查，6 bug + 27 risk 确认）
**已修的 bug/加固**：
- 暂存导入**原子幂等门闸**（建单后 `UPDATE staging SET imported WHERE imported IS NULL` rowcount 校验，防并发/重复导入建重复单，尤其 order_no 为空时）；"已导入" 400→409。
- 淘宝 create/update **校验 junfeng_order_id 存在且未软删**（否则 422），防悬空/挂到已删君丰单。
- 四个列表端点 **limit/offset 上下限**（`Query(ge,le)`，防超大 limit / 负值 DoS）。
- 汇率抓取**入库前区间校验 [5,50]** + 全程 **logging**（抓取失败/成功/循环异常）。
- **前端补 `覆盖¥`(jpy_override) 可编辑列**（三张表）——此前 override 这一核心功能在 UI 无入口。
- GotionCell **数值校验**（非数字不提交）+ **防 blur/enter 重复提交**（重复 PATCH 撞乐观锁 409）。
- LIKE 搜索 **autoescape**（% _ 通配）；列布局 **table_name 白名单**；IntegrityError 提示文案放宽（不再只说订单号重复）；**CORS allow_credentials=False**（令牌走 header 不用 cookie）；启动**校验 SECRET_KEY** 不安全默认值→醒目 WARNING。
- 删除 money.js 死代码；前端删除/忽略/导入等变更**统一 try/catch**（消除未捕获 rejection）；暂存 saveCell 仅 409 才整页重拉。
- 新增 **`backup.sh`**（WAL 安全 `sqlite3 .backup` + 保留 30 份），`backups/` 已 gitignore。

**部署前置（上公网/多人前必做，当前本地可接受）**：
1. **SECRET_KEY**：`.env` 设强随机（start.sh 首次已自动生成；勿用默认）。
2. **admin 弱口令**：默认 `admin/admin123` 仅本地；部署前改密（`SOROBAN_ADMIN_PASS`），别把 demo.py 带上生产。
3. **HTTPS + 登录限流**：反代层（nginx/caddy）加 TLS 与基础限流（登录端点无限流）。
4. **CORS_ORIGINS**：收敛为精确前端域名。

**已知技术债 / 待办（记录，不急）**：
- **Alembic**：仍用 `create_all`。**下次改模型且已有真实数据前必须引入 Alembic**，否则新列/改约束不生效。
- **备份**：用 `backup.sh` 挂 cron；重要数据可上 litestream。
- **时区口径**：`created_at/updated_at` 存 UTC；"今天/本月"判断用 JST；看板按业务 `date`（无 tz）。统一以业务 date 或显式 JST 为准。
- **jpy_auto/settled 是应用维护列**：靠"机器人只写暂存表"约定保证一致；引入君丰 bot 前不得让其直接写正表。
- **暂存无乐观锁**：人工+将来机器人写同一行末写胜；接机器人前给 staging 加 version 或约定 bot 只写空字段。
- **审计字段**：将来多角色/机器人可加 `created_by/updated_by` hook（当前 `source`+`payer_id` 已部分覆盖）。
- **bcrypt<4.1** 为兼容 passlib 的钉子；将来迁 argon2/直接 bcrypt 时处理；requirements 用 `>=`，可加 lock 文件。

### 第八版：新建订单自动写入当天汇率 + 暂存加 fx_rate 列（为迁移记录）
- 淘宝/君丰**新建时**若未给 fx_rate，后端自动填 `current_rate()`（当天汇率）；显式传的汇率不覆盖。于是 CNY 一填就能自动算日元。
- **暂存表 `TaobaoStaging` 新增 `fx_rate` 列**（含 [5,50] 校验）：手动新建/将来机器人抓取时都记下当天汇率；前端暂存页加「汇率」列。
- **导入迁移**：从暂存导入账本时，`fx_rate = 暂存记录的汇率 or 当天汇率`——优先用抓取当天记下的那个，历史才准（不是用导入当天的）。
- 自测：后端 7/7（三处 create 自动填、显式不覆盖、暂存保留、导入迁移 25.5 而非当天、settled 用迁移汇率）；headless 4/4（暂存汇率列、新淘宝行自动填 23.88）。

### 第九版：已导入行 暂存↔账本 双向联动（单一真源）+ 订单状态拆列 + 点物品即展开
- **点「物品」列即展开**：`NotionTable` 加列标记 `expand: true`，点这类单元格 toggle 该行详情面板（淘宝页 + 暂存页的物品编辑区）。原展开箭头照常。
- **已导入行两页都能改、且联动**（用户选「两页都能改」）：**不做两份拷贝互相同步**（那才是易出 bug 的互锁），而是**单一真源**——导入后账本订单是唯一数据：
  - **读**（overlay）：`staging.py::_read` 对已导入行用关联账本订单的实时值覆盖显示（下单日期/订单号/店铺/淘宝号/快递号/人民币/汇率/订单状态/物品），两页永远一致。
  - **写**（write-through）：改已导入的暂存行时，共享字段写穿到账本（`_SHARED_TO_ORDER` 映射，`order_date→date`），`compute_money()` 重算、`order.version+1`（让账本页乐观锁能察觉）；未导入行仍改暂存自身副本。
  - order_no 写穿撞账本唯一索引 → 全局 409；不回写暂存自身 order_no，避免误撞暂存去重索引。
- **状态拆成两列**：暂存原来的单一「状态」是**导入工作流**（待处理/已导入/已忽略），无法映射账本；现拆为 **导入状态**（工作流，留暂存）+ **订单状态**（`order_status`：已付/已发/…，与账本 `status` 联动）。新增 `TaobaoStaging.order_status`；导入时 `order.status = row.order_status or 已付` 一同迁移。**这是为爬虫铺路**：将来爬虫更新暂存行的订单状态即可同步进账本（爬虫不频繁，故不做实时）。
- **修订早前约定**：这**有意放宽**「机器人只写暂存、绝不动正表」——但仅限**已导入**行（人已确认入账），共享字段经暂存写穿到账本属预期；未导入行仍只写暂存。相应 [第二轮] 的「导入后变更告警」需求由此联动直接满足。
- 自测：后端 TestClient 23/23（导入迁移订单状态、overlay 覆盖、双向写穿、导入状态不被污染、jpy 重算、version bump→过期 409、order_no 撞唯一→409、未导入行本地存/导入迁移）；headless E2E 10/10（两状态列、导入、暂存改状态→账本、账本改状态→暂存 双向联动、无报错）。

### 第十版：菜单改名「全部订单」+ 表格加 ID 列 + 行号删除图标去重
- **改名**：菜单/路由/页面标题「全部淘宝订单」→「全部订单」（仅显示文字；`/staging`、`TaobaoStaging`、`/api/staging` 等代码/表名/接口**均不动** —— 用户明确「大改名先搁着」）。「淘宝订单」保持不变。
- **最左单列 = ID（合并了行号 / 新建 / 删除）**：几轮迭代后定型——`NotionTable` 最左是一个固定 56px 列（非拖拽、不入布局系统、位置稳定），表头「ID」：
  - 数据行显示 `row.id`（数据库真实主键）；悬停该格 → 变红色垃圾桶 → 点击删除（各页绑 `@delete=delRow/doDelete`，带确认弹窗）。
  - 幽灵新建行的「＋/✓」按钮也在此列首格（`commitNew`）。
  - **去掉了原「行号」列**（用户：行号不需要，删除和 ID 合并成一格）。
- **「操作」列瘦身**：淘宝/君丰/杂项的操作列原本只有「删除」，删除既已并入 ID 格，这三页整列移除（无 `#actions` slot → `hasActions=false`）；全部订单的操作列保留「导入/忽略」，宽度 176→128。
- 自测：headless 16/16（四页最左即 ID 列无行号残留、数据行首格 gtn-td-id、淘宝首行 ID==后端 id、幽灵「＋」在 ID 首格、ID 格垃圾桶删除真生效 行数-1、无报错）。
- 修复两处（headless 8/8）：① 幽灵新建行里空的展开/操作格无边框 class → 网格竖/横线断，给 `.gtn-new td` 补 `border-right/bottom`（有展开列的三页可见）；② 「全部订单」菜单图标名 `Inbox` 在 @element-plus/icons-vue **不存在**（故不渲染）→ 改 `Tickets`（Layout nav + router meta 同步）。

### 第十一版：君丰页「点选式」关联淘宝订单（双向编辑，同一外键）
- **需求**：淘宝先买、集运后建，故「JF 里挑淘宝单」比「每个淘宝单去点选 JF」更贴流程。JF 页展开面板从只读改成**可编辑**：已关联淘宝单每行显示 **订单号 · 物品×数量 · 结算金额** + 「移除」；下方一个 el-select 点选下拉「＋添加淘宝订单（未挂靠）」，选项同样显示订单号·物品×数量·结算金额。淘宝页「君丰(点选)」列**保留**（两页都能改）。
- **为何双向安全**：JF↔淘宝是**同一个外键** `TaobaoOrder.junfeng_order_id`，两页改的是同一字段，不存在暂存↔账本那种「两份拷贝分叉」。
- **后端**：`POST/DELETE /api/junfeng/{jf}/taobao/{tb}` 逐单增删；`GET /api/taobao?unassigned=true` 只列未挂靠；`TaobaoBrief` 加 `items`；`_read` 用 `selectinload` 载入子物品。attach 只接未挂靠的单，已挂别的 JF → 422（防误抢，先移除再加）。
- **多智能体对抗审查（3 维度 reviewer→逐条 skeptic 复核）发现并修复**：
  - 🔴 attach/detach 原为「读-判断-写」+ 裸 `tb.version += 1`，**并发下会静默双挂/误抢**、且丢失并发的 version 自增（削弱乐观锁）。→ 改成**原子守卫 UPDATE**：`UPDATE ... WHERE junfeng_order_id IS NULL AND deleted_at IS NULL` 靠 rowcount 判定（0→422），`version = version + 1` 在 DB 层自增，与 `guarded_bump` 同风格。
  - 🟡 `TaobaoBrief` 加 items 引入子物品 N+1 → `selectinload` 批量载入。
  - （对抗复核否掉 1 条误报：淘宝页 `.tag` 死 CSS 属改动前既有，不算本次缺陷。）
- 自测：后端 TestClient **20/20**（增删/幂等/422/404/version→409/unassigned/面板带物品金额）；**并发压测 20/20**（同单同时挂 A/B，恒为「一个 200 + 一个 422」，静默双挂 0、无 500）；无头 E2E **8/8**（点选加入→面板+后端外键、移除→清空、下拉显示物品金额、无报错）。
- **君丰表加「淘宝订单」列**（可拖动列，显示 `N 单：订单号…` 摘要，空时「点击添加」）：点该格即展开上面的关联编辑面板（复用 `expand:true` 机制，与淘宝/暂存页点「物品」展开同源）。headless 8/8。

### 第十二版：退款/取消不计入合计（弃用负数冲抵）+ 全面禁负数
- **记账模型调整**（用户定）：不再「额外开一行、用负数冲抵退款」。订单打上 **退款 / 已取消** 标记即**自动不计入看板合计**——金额与物品仍照常显示，只是不加总。
- 后端：`models.CANCELLED_STATUSES` → `EXCLUDED_STATUSES`（并入淘宝 `退款`）；`dashboard._valid_conds` 用它 `status.notin_(...)` 排除求和/计数/按月。君丰仅 `已取消`（无退款态），杂项无 status 列不受影响；暂存本就不进看板。
- **全面禁负数**（「系统不需要负数」）：`MoneyIn.price_cny / jpy_override` 与 `StagingBase.price_cny` 加非负校验（<0 → 422，提示「退款/取消请用状态标记」）；0 与正数照常。覆盖淘宝/君丰/杂项/暂存四处。
- demo 去掉两条负数示例：淘宝 `TB250704050` 退款 `-25→+25`（演示「退款照显但不计入」）；杂项「超卖退款到账 `-500`」→「关税补缴 `+650`」。
- 自测：后端 TestClient **14/14**（退款/取消不计入合计与计数、金额仍算出、列表仍显示、四处禁负数 422、0/正数放行）；headless **7/7**（看板渲染、退款单正金额+标签仍显示、负数输入弹错且不落库、无报错）。

### 第十三版：「君丰订单」全量重命名为「集运订单」（Shipment）
- 去掉供应商专属词，统一为通用的「集运订单」。**代码/表/接口/变量全部重命名**，一次性大改（用户明确要求）。
- 命名对照（本文档以上所有 `君丰/Junfeng/junfeng_*` 均为当时命名，现行代码一律用右侧）：

  | 旧 | 新 |
  |---|---|
  | 君丰订单（显示） | 集运订单 |
  | `JunfengOrder` / 表 `junfengorder` | `ShipmentOrder` / 表 `shipmentorder` |
  | `JunfengStatus` / `JUNFENG_STATUS` | `ShipmentStatus` / `SHIPMENT_STATUS` |
  | `JunfengCreate/Update/Read/Base` | `ShipmentCreate/Update/Read/Base` |
  | `shipment_no`←`junfeng_no` | `shipment_no` |
  | `junfeng_order_id`（淘宝外键） | `shipment_order_id` |
  | `junfeng_order`（关系） | `shipment_order` |
  | `/api/junfeng`（+ `/{id}/taobao/{tb}`） | `/api/shipment` |
  | `Source.junfeng_bot` | `Source.shipment_bot` |
  | 看板 `junfeng_jpy/junfeng_count` | `shipment_jpy/shipment_count` |
  | `routers/junfeng.py` | `routers/shipment.py` |
  | 前端 `views/Junfeng`、`junfengApi`、`shipmentOptions`、`table-name/布局键 junfeng` | `views/Shipment`、`shipmentApi`、`shipmentOptions`、`shipment` |

- 方式：对 `backend/app`+`frontend/src` 全部 `.py/.vue/.js` 做大小写一致的整词替换（`junfeng→shipment`、`Junfeng→Shipment`、`JUNFENG→SHIPMENT`、`君丰→集运`），共 15 文件；两文件改名；无真实数据故 `create_all` 直接重灌演示库。docs 历史日志保留旧名作当时记录。
- 自测：后端启动+重灌 OK，`/api/shipment` 200 / 旧 `/api/junfeng` 404，看板出 `shipment_jpy`；代码残留 junfeng/君丰 = **0**；后端 TestClient **20/20（attach/detach 边界）+ 14/14（退款/禁负）**、并发 **20/20**、无头 E2E **11/11**（菜单=集运订单无君丰、页面/URL、点选关联打 `/api/shipment/{id}/taobao/{tb}` 写 `shipment_order_id`、淘宝页「集运(点选)」列、看板、0 报错）全部对新接口重跑通过。

### 第十四版：可管理的「标签」列（列头增删选项 + 单元格下拉选）
- **需求**：淘宝号（账号 a/b…）是固定几个号，做成标签列——列头能加/删标签，行内拉下拉选。集运订单加「收货人」列，同款机制。
- **后端**：新表 `TagOption(id, field, value)` + `UNIQUE(field, value)`；`routers/tags.py`：`GET/POST/DELETE /api/tags/{field}`（列出/去重添加/删除；添加用 sqlite `on_conflict_do_nothing` 原子去重、空值 422；删除只把标签移出可选集、**不动已用该值的历史行**）；字段白名单 `{taobao_account, recipient}`，未知字段全 422。`ShipmentOrder` 加可空 `recipient` 列（+ 三个 schema）。demo 预置 `taobao_account=[acctA,acctB]`、`recipient=[本人,家人,朋友]`。
- **前端**：`NotionTable` 支持 `type:'tag'`+`field` 列：挂载时按字段拉取选项到 `tagOptions`；`cellCol()` 把标签列映射成 `select`（复用 GotionCell，渲染成 tag+下拉）；**列头** el-popover（齿轮图标，`@mousedown.stop` 防触发列拖拽）里管理该字段标签（可关闭的 el-tag 删除 + 输入框添加）。`api/tags`。
- **页面**：淘宝 + 全部订单 的「淘宝号」列 text→tag（**共享同一个 `taobao_account` 标签集**）；集运订单加「收货人」tag 列。
- 设计取舍：标签单色（灰）不做配色；删标签不改历史行值；筛选框仍是原来的输入框（未改成下拉）。
- 自测：后端 TestClient **13/13**（列出/去重幂等/空值 422/字段隔离/删除/三动词白名单/集运 recipient 增改清空）；无头 E2E **11/11**（淘宝号渲染成标签、列头齿轮开管理弹窗列出 acctA/acctB、加 acctC 出现且持久化后端、单元格下拉含新标签且可选中、集运「收货人」列+标签+列头管理、0 报错）。

### 第十五版：全项目对抗审查 + 修复（6 维度审查，15 确认，修 14 / 留 1）
两个后台 workflow：标签功能审查（1 确认）+ 全项目审查（6 维度→逐条 skeptic 复核，25 raw→15 确认：1 高 9 中 5 低）。合并去重后**修 14 条**：
- **金额一致性**：`special_fee_jpy` 是唯一漏网的金额输入（无非负校验，负值会静默压低 jpy_settled/看板合计）→ ShipmentBase/Update 加 `_nonneg_fee`（<0→422）。
- **写穿乐观锁**：暂存写穿账本原来用裸 `order.version += 1`（绕过守卫，并发下丢失更新/吞 version 自增）→ 改走 `guarded_bump`（原子自增+409），与其余账本变更同一套。
- **删单可重导**：从暂存导入的淘宝订单被软删后，暂存行永远卡「已导入」且不可重导 → 淘宝 `delete_order` 清暂存行挂靠、状态回「待处理」（对齐集运删除清子外键的做法）。
- **挂靠竞态**：`attach_taobao` 原子 UPDATE 加 `EXISTS(集运单未软删)` 子查询，防极小窗内集运单被并发删导致挂到已删单。
- **错误可读性**：① axios 拦截器展平 FastAPI 422 的 `detail` 数组，不再把「金额不能为负/汇率越界」等具体原因吞成通用「请求失败」；② 全局 IntegrityError 409 改成因果无关文案 + 记 `exc.orig` 到日志（不再误导为「单号重复」）。
- **前端健壮**：③ 列布局拉取完成前（`layoutReady`）禁用列拖拽/拖宽，杜绝加载窗内用默认序覆盖已存布局；④ 标签拉取失败不再整会话卡空——列头弹窗 `@show` 重试（`loadTag`）。
- **可读性/统一**：⑤ 清掉重命名遗留的 `jf` 词（后端 `jf_id/jf`→`shipment_id/shipment`、Query 描述「JF 页」→「集运页」；前端 `jfRow/jfId`→`shipment*`、`.jf-*` CSS→`.ship-*`）；⑥ `STAGING_STATUS/stagingTag` 归拢到 `constants.js`；⑦ 删 Shipment 死 CSS `.tag`；⑧ NotionTable 魔数 160/56/30 抽成 `DEFAULT_COL_W/ID_COL_W/EXPAND_COL_W`。
- **有意保留 1 条**：`useLedgerTable` composable（4 页 CRUD 脚手架去重）——真 DRY，但对刚经历大改名的工作代码做四页级重构，风险>当下收益，记为后续。demo.py 里 `jf1/jf` 是一次性 seed 局部名、无 API/模型面，一并保留。
- 自测：后端 TestClient **11/11**（特殊费禁负、删单后暂存回待处理+可重导、写穿 version 各+1 不吞增、写穿后旧 version→409、写穿仍正常）+ 既有 **20/20+14/14+13/13** 全过无回归；无头 E2E rename **11/11** / tags **11/11** / refund-smoke **7/7**。

### 第十六版：暂存表补乐观锁（version）——为「爬虫多次更新同一行」铺路
承接审查里「暂存无 version」那条：用户指出爬虫会反复更新同一暂存行，遂**给暂存表加 `version` 乐观锁**（此前它是唯一无锁的表）。
- `guarded_bump` 泛化：`hasattr(model, "deleted_at")` 才加软删条件，使其可用于硬删的暂存表（`TaobaoStaging` 无 `deleted_at`）。
- `TaobaoStaging` 加 `version`；`StagingRead`/`StagingUpdate` 带 `version`（更新必填）。
- `update_staging` 先 `guarded_bump(TaobaoStaging, version)`（stale→409），再走已有的写穿账本逻辑（已导入行仍额外 `guarded_bump` 账本）；`import`/`ignore`/以及淘宝删单清暂存挂靠都 `version+1`，保持单调。前端暂存页 saveCell/saveItems 带上 `row.version`、409 刷新（冲突提示改通用文案）。
- 价值：将来爬虫反复 upsert 同一行时，人若拿旧 version 编辑会 409-刷新而非静默覆盖；也让暂存与其余账本表一致。
- 自测：后端 TestClient **16/16**（新建 v=1、正确 version→200+自增、旧 version→409、缺 version→422、import/ignore/删单各自 version+1、已导入行写穿需暂存 version 且账本 version 也+1、删后可重导）；curl 实测运行服务器：带 version 编辑落库、旧 version→409；既有 **20/20+14/14+13/13+11/11** 全过无回归。

### 第十七版：标签调色盘（管理型标签列自动配色）
- 需求：标签能有颜色。做成**预设调色盘 + 按值哈希自动配色**（用户认可「预设一套 7-10 个就够」）。
- `constants.js`：`TAG_PALETTE`（10 色，暗色主题柔和底色 `rgba(...,.18)` + 同色系边框/文字）；`tagColor(v)` 用简单字符串哈希 `h=h*31+c` 映射到固定一色（同名同色、无需存储、循环）；`tagStyle(v)` 返回 inline 样式串。
- 只作用于**管理型标签列**（`type:'tag'` → cellCol 标记 `tagColored`）：`GotionCell.tagAttrs(v)` 标签列用调色盘 `:style`、其余状态列仍用语义 `statusTagType` type；`NotionTable` 列头管理弹窗的标签也上色。`状态/导入状态` 等语义列不受影响。
- 自测：headless **6/6**（淘宝号 acctA/acctB 各带调色盘 inline 色且不同、`状态`仍语义 type、收货人上色、无报错）+ 截图肉眼确认。纯前端、DB 未动。
- 备注：当前是**自动配色**（按值哈希），非手动指定；若要「某标签手动挑色」需给标签存颜色字段 + 列头取色器，留待需要时加。

### 第十八版：淘宝订单状态对齐淘宝交易状态（方便爬虫对接）
- 原 `已付/已发/已收/退款/已取消` → 对齐淘宝交易状态：**待付款 / 待发货 / 待收货 / 交易成功 / 退款 / 交易关闭**（新增「待付款」，其余按淘宝措辞改名）。爬虫抓到的状态几乎 1:1 映射。
- `TaobaoStatus` **枚举成员名保持不变**（`unpaid`(新)/`paid`/`shipped`/`received`/`refunded`/`cancelled`），只改 value——所以引用成员名的代码（`TaobaoBase.status` 默认值、`EXCLUDED_STATUSES`）自动跟随。
- **看板排除**加入 `待付款`（未付款不计入），与 `退款`/`交易关闭` 一并排除；金额与物品仍照常显示。
- 前端 `TAOBAO_STATUS` 六态 + `statusTagType` 配色（待发货=warning/待收货=primary/交易成功=success/退款=danger/待付款·交易关闭=info）；demo 状态整体重映射（`status="X"` 精确替换也覆盖 `order_status="X"`，不误伤集运「已发出」），并留一条暂存 `待付款` 演示新态。集运（打包中/已发出/已签收/已取消）不动。
- 自测：看板 7/9 计入、合计 ¥26637 不变；旧值 `已付`→422、新值 `待发货`→200；后端 attach **20/20** · refund **15/15**（含待付款排除）· tags **13/13** · audit-fixes **11/11** · staging-lock **16/16**；前端 tagcolor **6/6** + refund-smoke **7/7**，截图确认六态渲染+配色。

### 第十九版：标签风格统一为「柔和底色」
- 用户偏好账号标签那套柔和底色，遂把**所有标签统一成同款观感**：`constants.js` 加 `typeStyle(type)`（EP 语义 type→柔和底色，复用调色盘蓝/绿/琥珀/红 + 灰）+ 派生 `statusStyle`/`stagingStyle`。
- 状态标签（GotionCell 状态/订单状态格）、暂存「导入状态」+「已导入」徽标、侧栏汇率「旧」徽标，都从实心 `type + effect=dark` 改为柔和底色 `:style`；账号/收货人标签本就是这套。**语义色保留**（退款=红、交易成功=绿…），只是观感统一。
- 自测：headless **8/8**（账号=调色盘、状态=柔和底色 inline、退款=红调、交易成功=绿调、收货人上色、无报错）+ 截图确认全表标签风格一致。纯前端、DB 未动。

### 第二十版：界面打磨（下拉/日期区间/去大标题）
- **下拉菜单更清爽**：GotionCell 的 select/tag 弹层——选中项由原来的「整行蓝底框」（与标签胶囊双重描边、发糊）改成**右侧绿色对勾**；弹层加 `popper-class="gtn-pop"`（App.vue 全局：更深底色 `#16203a` + 收紧内边距 6px + 阴影），「清除」行加 × 图标。
- **日期区间筛选收窄**：`el-date-picker daterange` 在 flex 工具栏里默认被拉伸到 ~680px，霸占整条工具栏。加 `.flt-date` 全局类锁死 `width:244px; flex:0 0 244px`（弹性容器里光设 width 会被 flex-grow 撑开，故须一并锁 flex），并收紧区间分隔符内边距。三张有日期筛选的页（淘宝/集运/杂项）统一。
- **去掉每页大标题**：五页 `<h2 class="page-title">` 全删（含 App.vue 里 `.page-title` 规则）；全部订单页保留那行说明 hint，只是缩小。内容直接从工具栏/卡片起，更省纵向空间。
- 自测：daterange 实测宽 **244px**；下拉选项改值→落库→刷新仍在 **3/3**；标签渲染回归 **8/8**；四页截图确认无标题空档、无页面报错。纯前端、DB 未动。

### 第二十一版：多智能体对抗审查 + 修复（并发/契约/健壮性）
6 维度对抗审查（评审→对抗验证）+ 一次专项并发复审（首轮该维度全 6 次重试都 stall，用 sonnet 补跑）。共确认并修 4 处：
- **前端建单必失败（high，契约漂移）**：`Taobao/index.vue` addRow 写死 `status:'已付'`——状态对齐淘宝后（第十八版）该值已非法，后端 `_check` 校验 → 422，幽灵行建单 100% 失败。改为**不写 status**，让后端 `TaobaoBase` 默认「待发货」生效，根除枚举改名后的前端残留漂移。（4 维度独立命中同一处）
- **`today()` UTC 偏差（robustness）**：三页 `new Date().toISOString().slice(0,10)` 取的是 UTC 日；用户在日本(JST)，凌晨 0~9 点新建会记成前一天。改用本地时区拼日期（淘宝/集运/杂项三页）。
- **`ignore_staging` 绕过乐观锁（high，并发）**：原来 `row.version += 1` 是 Python 读-改-写、无 `WHERE version=?` 守卫，与其余所有写端点不一致 → 并发忽略/爬虫写丢失 version 自增。改为 **DB 层原子自增**（`sa_update ... version=version+1`，与 import 门闸同风格）。
- **`create/update_order` 集运存活校验 TOCTOU（medium，并发）**：`_check_shipment` 是写事务开启前的裸读，并发软删集运单会留下悬空外键。`attach_taobao` 早有 EXISTS 守卫、这两条 PATCH/POST 没有。修法：把校验挪到**写事务内**——update 放到 `guarded_bump` 之后、create 放到 `flush()` 之后（WAL 单写者串行化 + 同事务快照，闭合竞态；被拒时整事务回滚，不留悬空、version 不虚增）。
- 审查判为「无需改」：`SECRET_KEY` 仅告警不 fail-closed（计划 P9 明确本地开发默认值，fail-closed 会破坏无 .env 的本地流程）；`GotionCell.norm()` 小数以字符串下发（反而更精确，避免 float 化）。
- 自测：后端 TestClient 集成 **12/12**（含建单默认状态、软删集运单挂靠 422、被拒事务回滚、旧 version→409、ignore 原子自增/404）；建单默认状态 **4/4**；前端 E2E 建单 **7/7** + 忽略 **4/4** + 标签回归 **8/8**。改后重置演示库、重启后端。

**确认复盘（针对首轮 stall 的补审）**：因首轮多为 opus-1M 在高负载下 no-progress stall，用 sonnet 另跑一轮「修复确认 + 全量新扫」。4 处修复独立复核全部 present+correct（ignore 修复还跑了 20 线程并发压测：version 精确到 21、零丢失）。新扫又抓出 2 处首轮被 stall 掩盖的问题，一并修掉：
- **必填状态可被「清除」→ 误导 409（high，健壮性）**：GotionCell 的 select 弹层对所有列都渲染「清除」，但淘宝/集运的 `status` 是非空列，点清除会发 `status=null` → 撞 NOT NULL → 409「数据完整性冲突」。加 `col.clearable !== false` 门控，并给两处 `status` 列标 `clearable:false`；标签列与可空的 `order_status` 仍可清除（已验证 order_status=null→200）。
- **`statusTagType` 漏掉 `已取消`（low，可读性）**：集运「已取消」原靠 `|| 'info'` 兜底，补显式 `已取消:'info'`（与「交易关闭」同为终态灰）。
- 补审自测：清除门控 E2E **4/4**（状态无清除/6 选项在/标签有清除/无报错）+ 标签回归 **8/8** + 建单 **7/7**。

### 第二十二版：标签下拉「随数据自增」（爬虫友好）
- 原逻辑：下拉可选值只来自 `TagOption` 表（列头手动增删）——爬虫把新账号/收货人写进订单后，值虽在格子里显示，却**不会自动出现在下拉**，得再手动登记一次。
- 改为 `/api/tags/{field}` 返回 **手动标签 ∪ 数据里实际出现过的值**（`tags.py` 加 `_FIELD_SOURCES`：淘宝账号看 `TaobaoOrder`+`TaobaoStaging`、收货人看 `ShipmentOrder`，`SELECT DISTINCT` + 软删过滤）。这样**爬虫只要把值写进库（走 API 或直写库都行），就自动成为可选项**，无需再碰 TagOption。
- 顺序：手动标签在前（保留列头顺序、含未被使用的预设），数据派生值去重后按序追加。
- 取舍：删除某手动标签只是移出预设；若该值仍被数据使用，会因「数据里出现过」继续可选（在用的值本就该能再选，不能凭空消失）。
- 自测：后端 TestClient **9/9**（爬虫写订单/暂存的新账号自动出现、集运收货人自动出现、手动预设保留且在前、值为纯字符串、软删该行后派生值消失）；前端 E2E **3/3**（模拟爬虫直接 API 建单 → 新账号即时出现在下拉 → 清理还原）。改后重启后端、演示库保持 9/3/4 干净。

### 第二十三版：状态配色对调 + 格子下拉还原标准样式
- **待发货 / 待收货 配色对调**：`statusTagType` 里 待发货 `warning→primary`（蓝）、待收货 `primary→warning`（橙）。单一真源，淘宝状态列与暂存「订单状态」列一并生效。
- **格子下拉还原标准样式**：撤掉第二十版给 GotionCell select 弹层加的自定义外观（`popper-class="gtn-pop"` + 深底 `#16203a` + 对勾图标 + 紧凑 padding），回到默认 el-popover 样式，与工具栏 el-select 等其它下拉一致。保留上一版的「必填字段不可清除」门控（`col.clearable !== false`），「清除」回到纯文本。移除 App.vue 的 `.gtn-pop` 规则与 GotionCell 里 `Check/Close` 图标导入（日期区间 `.flt-date` 规则保留）。
- 自测：**6/6**（待发货=蓝 rgba(59,130,246)、待收货=橙 rgba(245,158,11)、无 gtn-pop 类、选项完整、无报错）+ 清除门控回归 **4/4**（状态无清除/标签有清除）。纯前端、DB 未动。
- 补：应「好确认」诉求，在选中项右侧**加回绿色对勾 ✓**（`.gtn-ck`，`margin-left:auto` 顶到右侧），但只加对勾、不恢复第二十版那套自定义弹层容器——保持标准 el-popover 外观。自测 **3/3**（有且仅一个✓、落在当前值上、无报错）。

### 第二十四版：淘宝页「集运(点选)」下拉优化
- 原下拉选项是「单号 / 日期·状态·运费」一行灰字，状态是纯文本、与全站彩色状态标签不一致；无运费时显示「运费—」略生硬。
- 改成两行：**单号（粗）+ 状态彩色标签**（`statusStyle(j.status)`，柔和底色，与状态列一致）｜ 次行 `日期 · 运费 X`，无运费显示**「待定」**。
- el-select 下拉默认项高 34px 会夹住两行，加 `popper-class="ship-pop"` + App.vue 全局 `.ship-pop .el-select-dropdown__item{height:auto;line-height:1.3;padding:6px 12px}` 自适应高度。取值绑定/change 保存逻辑未动。
- 自测 **4/4**（每项带状态标签、标签走柔和底色 rgba、无运费显示「待定」、无报错）。纯前端、DB 未动。

### 第二十五版：标签配色改「按序号」取色（修撞色）
- 症状：加了几个标签就出现两个同色。根因：`tagColor` 是 `hash(值) % 10`，10 个桶哈希极易撞（生日悖论，加三四个就可能重复），白白浪费了 10 色调色盘。
- 改为**按标签在该字段可选集里的序号取色**：新增 `tagStyleAt(index, value)`，第 1/2/3… 个取调色盘第 0/1/2… 色，前 10 个保证互不相同；序号 `<0`（值暂不在集里，如首帧未加载）回退按值哈希，仍是确定色不闪。
- 接入：`GotionCell.tagAttrs` 用 `col.options.indexOf(v)` 取序号（单元格与下拉同源同色）；`NotionTable` 列头标签管理用 `v-for` 的 `i`。取色不再依赖字符串哈希。
- 取舍：颜色跟「序号」走，故增删/重排标签会改变其后标签的颜色（可空但少见；手动预设按加入顺序稳定）。
- 自测：单元 —— 旧哈希法 6 个样例标签只得 **5 色（撞 1）**、新序号法 **6/6 全不同**；前 10 序号 **10/10 全不同**。E2E —— 加到 7 个收货人标签渲染 **7/7 不同色**（蓝/绿/琥珀/红/紫/粉/青，按调色盘顺序）+ 单元格标签回归 **8/8**。纯前端、DB 未动。

### 第二十六版：标签颜色持久化（稳定不变）+ 使用中不可删 + 集运格显示状态/✓
三个诉求一起做：
- **① 颜色稳定**：第二十五版按「序号」取色仍会因增删标签而重排、改动他人颜色。改为**把颜色序号持久化到 `TagOption.color`**——建标签时用 `_pick_color`（取最小未用序号，前 10 不撞色）分配一次、之后不再变。`database.py` 加 `_add_missing_columns` 迁移（`ALTER TABLE tagoption ADD COLUMN color`，幂等），旧库无需重建。数据里出现过的值（订单/暂存/集运，排软删）由 `_sync` **自动登记为标签并分配颜色**（沿用第二十二版「随数据自增」）。
- **② 使用中不可删**：`GET /api/tags/{field}` 返回 `[{value,color,in_use}]`；`in_use=` 该值是否被数据使用。前端列头对 `in_use` 的标签隐藏「×」并提示「使用中的标签不可删除」；后端 `DELETE` 亦对在用值返回 **409** 兜底。
- **③ 集运(点选)格**：单元格用 el-select `#label` 插槽显示 **所选集运单号 + 状态彩色标签**（打包中/已发出/已签收）；下拉选中项右侧加 **✓**（`j.id === row.shipment_order_id`）。
- 前端：`NotionTable` 用 `tagMeta{值→{color,in_use}}` + `applyTags` 承接新返回；`GotionCell.tagAttrs` 用 `col.tagMeta[v].color`（缺失回退哈希）；`Taobao` 加 `shipById/shipNo` 助手 + `Check` 图标。
- 自测：后端 TestClient **12/12**（迁移加列、旧标签回填、7 标签全不同色、**加标签后旧色不变**、in_use、删在用→409/删未用→200、删后再加他人色不变、数据值自动登记）；前端 E2E **9/9**（集运格显示状态标签、下拉✓、颜色互不同、在用无×、有未用可删、提示、无报错）+ 回归 建单 7/7 / 标签 8/8 / 清除 4/4。迁移在真库跑通、后端已重启。
- **对抗审查补 3 处**（sonnet 复审，3 确认 0 误报）：
  - (medium) `_data_values` 把**已忽略的暂存行**也算「在用」→ 丢弃的抓取会误锁账号、误自动登记。加 `status != 已忽略` 过滤。
  - (low) `_sync` 自动登记用的是普通 insert，并发首见同值会撞唯一键 → GET 竟可能抛 409。改用 `on_conflict_do_nothing`（与 add_tag 一致），GET 幂等安全。
  - (medium) `NotionTable.removeTag` 吞掉 409：http 拦截器**刻意跳过 409**，删在用标签（stale 竞态下 × 仍在）会静默无反馈。改为自行 `ElMessage.warning` 提示。
  - 补测：后端 **8/8**（已忽略暂存不算在用/可删、重复 GET 幂等、稳定性不变）；前端 E2E 回归 9/9 + 3/3。

### 第二十七版：集运(点选)去掉箭头/清除叉 + 清除进列表置顶 + 打包中置顶排序
- **单元格不再显示下拉箭头(V)与清除叉(×)**：去掉 el-select 的 `clearable`，`.el-select__suffix` 隐藏——避免误触清空。单元格只留「单号 + 状态标签」，点一下即开。
- **清除移进下拉列表、固定最上**：加一条 `:value="-1"` 的「清除（取消集运）」项置顶（仅在已挂集运时显示），点它即解绑；`onPickShipment` 把 `-1` 映射为 `null`。与其它下拉「清除在列表里」一致，但因集运单可能很多，放最上免滚动。
- **打包中永远置顶**：`sortedShipments` 计算属性把 `status==='打包中'` 的排到最前（稳定排序，同组保持日期倒序），常挂新单的活跃集运单一眼可见。
- 自测 E2E **7/7**（无箭头/叉、清除置顶、打包中旧日期也排到已发出之前、前两项皆打包中、点清除→未集运、重选还原、无报错）。造了个旧日期打包中临时单验排序、测后已删，演示库回 3。纯前端、DB 未动。

### 第二十八版：集运页点订单号 → 跳到淘宝页并展开该单（方案 A：隔离+展开）
- 集运页关联淘宝订单表的 `订单号` 改成可点链接（`el-link`）→ `router.push('/taobao?focus=<id>')`；用**订单 id**当钥匙（兼容无订单号的单）。
- 淘宝页 `watch(route.query.focus, {immediate})` → 把列表**隔离成该单**（后端 `GET /api/taobao` 加 `id` 筛选参数，与软删/分页正确叠加）并**自动展开**；顶部显示可关闭的「定位订单 #X · 点 × 看全部」胶囊，点它 `router.replace` 回完整列表。找不到（已删）时给「未找到该订单」提示。
- `NotionTable` 加可选 `openId` prop：`watch(openId,{immediate}) → open.add(id)`；行数据到达后（`v-if open.has`）即渲染为展开态，天然处理「先设 id 后到数据」的时序。附加式改动，对其余 3 个用到 NotionTable 的页零影响（回归验证）。
- 设计权衡：选了「隔离显示该单」而非「完整列表里翻页高亮」——后者要后端算页码、易与排序/筛选打架，bug 风险高得多。
- 自测：后端 `id` 筛选（命中 1 单 / 不存在→0）；E2E **10/10**（集运展开→点订单号→跳转带 focus→只显 1 单→就是该单→自动展开物品面板→胶囊在→点×回全部→胶囊消失→URL 去 focus→无报错）；回归 建单 7/7 / 三功能 9/9 / 标签 8/8 / 清除 4/4 / 集运下拉 7/7 全绿。
- **对抗审查补 1 处**（low）：`openId` watch 只加不删 → 依次定位 A、B 再「显示全部」会把 A、B 都残留成展开态、越积越多。改为 `watch(openId, (id, prev) => { open.delete(prev); open.add(id) })` 只收回自己上次自动展开的那行（不动用户手动展开的）。E2E 加断言「清除后无残留展开行」→ **11/11**。

### 第二十九版：下拉贴紧单元格 + 金额单位统一（元 / 円）
- **下拉贴紧**：GotionCell 的 el-popover 默认 `offset=12` 显得离太开，改 `:offset="4"`（实测 cell 底到弹层 4px）。全站格子下拉统一。
- **金额单位统一**：表头把含糊的「¥」统一成 **（円）**（日元）、CNY 列统一成 **（元）**——淘宝/暂存/杂项的 `人民币（元）`、集运的 `运费（元）/特殊费（円）/覆盖（円）/结算（円）`、覆盖/结算（円）等，含集运展开表的「结算（円）」。集运「特殊费」补 `format:'jpy'` 使其也按日元显示。
- **显示格式**：`money.js` 里 **CNY 用「￥」前缀**（￥123.00）、**JPY 用「円」后缀**（12,345円）。改一处 `fmtJPY`/`fmtCNY` 全站生效——单元格、看板卡片、按月表、集运点选下拉运费等。
- 自测：截图确认淘宝/集运/看板三处表头与数值（CNY ￥前缀、JPY 円后缀）；回归 建单 7/7 / 集运下拉 7/7 / 跳转 11/11 全绿。纯前端、DB 未动。

### 第三十版：空格占位提示（仅新建行）+ 表头「集运(点选)→集运订单」
- 各列空单元格加**引导占位**，但**只在幽灵新建行（第一行）显示**；普通数据行空值一律「—」。GotionCell 加 `newRow` prop + `emptyText` 计算（newRow 才用 `col.placeholder`，否则「—」，select 新建行默认「选择」）；NotionTable 幽灵行给 GotionCell 传 `new-row`，并为只读/自定义列（如集运订单）补渲染占位（`.gtn-new-ph`）。
- 占位：快递号→「快递号」、人民币（元）→「实付人民币」、汇率→「当天汇率」、覆盖（円）→「实付日元」、集运订单→「选择」。四页对应列一并加（集运运费列用「实付运费」、暂存 fx 由「当天」→「当天汇率」）。
- **结算列不显任何文字**（去掉占位；新建行不渲染只读列→空，数据行空值→「—」）。
- 表头 **集运(点选) → 集运订单**（淘宝页）。
- 自测：截图确认幽灵行显占位（订单号/选择/快递号/实付人民币/当天汇率/实付日元/集运=选择、结算空）、数据行空值显「—」；回归 建单 7/7、跳转 11/11、清除门控 4/4 全绿（e2e 里旧列名/空首行断言已随改名更新）。纯前端、DB 未动。

### 第三十一版：手机响应式——侧栏收成抽屉（参考 mercari_manage）
- `Layout.vue` 加 `matchMedia('(max-width:768px)')` 判手机。**手机**：侧栏变成从左滑入的抽屉（`position:fixed` + `translateX(-100%)↔0`，`Teleport to body :disabled="!isMobile"` 避免父级 transform 破坏 fixed），左上角浮动**菜单按钮(FAB)**开、**遮罩**/侧栏内**×**/**点菜单项**都能关；`@select` + `watch(route)` 保证任意方式导航都收起抽屉。**电脑**：220px 侧栏常驻、无 FAB，行为不变。
- FAB/遮罩 `Teleport to body` + 非 scoped 样式（z-index 5000/5010/5020）；`.content` 手机内边距收到 12px、顶部留 52px 给 FAB；`.layout` 加 `overflow:hidden` 防抽屉滑出造成横向滚动。
- 表格仍横向滚动（数据密、可接受）；看板卡片 `el-col :xs=12` 手机两列、登录卡自适应。
- 自测：手机 390×844 + 电脑 1400 双视口 E2E **15/15**（FAB 显隐、抽屉滑入 x=0、遮罩、点菜单导航+自动关、点遮罩关、无横向溢出、电脑侧栏常驻宽 220）；桌面回归 建单 7/7、跳转 11/11。截图确认抽屉观感。纯前端、DB 未动。

### 第三十二版：全项目对抗审查（7 维度）+ 修复
7 维度审查（并发/金额/安全/前端/契约/死代码/健壮性 → 逐条对抗验证，sonnet 无 stall），**6 确认 0 误报**，修 5 留 1：
- **(med) 暂存写穿只改账本、暂存行原始列陈旧**（staging.py）：编辑已导入暂存行的 `taobao_account` 只写到 order，`TaobaoStaging.taobao_account` 留旧值；而 `tags._data_values` / 列表筛选**直读暂存原始列**（`_read` 的覆盖只在响应层）→ 旧账号被误判「使用中」不可删、`q`/账号筛选对这类行失准。修：写穿时 `setattr(row, key, value)` 同步暂存行自身列。
- **(med) 删集运单解绑子订单未 bump version**（shipment.py）：`.values(shipment_order_id=None)` 漏了 `version+1/updated_at`，与本文件 attach/detach、taobao 删单清暂存 back-ref 不一致 → 客户端陈旧态下次 PATCH 不会 409、乐观锁失效。修：补 `version=version+1, updated_at`。
- **(low) 暂存 saveItems 任何错误都整表刷新**（Staging/index.vue）：非 409 也 `load()`、丢弃其它行未保存编辑。改为**仅 409 刷新**（对齐淘宝页）。
- **(low) 死代码**：taobao.py 未用的 `User` import 删除；constants.js 把仅内部使用的 `statusTagType/stagingTag/tagColor/tagStyle`（已被 statusStyle/stagingStyle/tagStyleAt 封装/回退取代）去掉 `export`。
- **(low) 留一处不改**：标签配色分配是读-改-写，并发加标签罕见撞色——纯观感、单用户几乎不可能触发，且严谨修复需序列化/唯一索引（而颜色 10 后本就重复，唯一索引会错），**加复杂度换零正确性收益，不划算**，故保留并记录。
- 自测：后端 TestClient **7/7**（编辑已导入行账号→旧账号可删/新账号占用、删集运单→订单 version 自增+旧 version PATCH 409）；前端回归 标签 8/8 / 建单 7/7 / 手机 15/15 / 跳转 11/11 全绿。改后重置演示库、重启后端。

### 第三十三版：淘宝爬虫脚手架（soroban 下但 git 排除）+ soroban 瘦触发端点/按钮
按用户要求「爬虫放 soroban 下但被 soroban 排除、入口按钮进项目」：
- **`soroban/scraper/` 下新建**（soroban `.gitignore` 用 `/scraper/*/` 排除各爬虫子目录、只留 `scraper/README.md`；各爬虫各自成库/venv/Playwright、不 import soroban、零污染源项目），先做 `scraper/soroban-scraper-taobao/`（各自成库、已连远程 Gosoki/soroban-scraper-taobao）：`config`（读 .env）、`soroban_client`（登录+按 order_no **幂等 upsert** 回灌暂存表，**真实可用**）、`session/fetch/normalize`（Playwright 登录/拦 mtop/字段映射的**诚实 stub**，标了 TODO，待抓包+账号才能填）、`run` CLI（`--demo/--login/--account`）、README/.gitignore。方案详解见 `docs/taobao-爬虫方案.md`。
- **soroban 瘦集成**（用户选「端点+按钮」）：`config` 加 `SCRAPER_CMD/SCRAPER_CWD`；新 `routers/scrape.py` 的 `POST /api/scrape` 按配置**列表形式启动子进程**（无 shell 注入、fire-and-forget）；全部订单页加「抓取淘宝订单」按钮。默认不配 → 按钮点了提示「未配置爬虫」（正确）。
- 自测：`--demo` 推演示单 **created 1**；同 order_no 二次推 **created→updated**（幂等不重复）；`POST /api/scrape` 配 demo 命令→`started:true`+暂存冒出 DEMO 行→清理回 4；未配置→**400**+友好提示；按钮 E2E **3/3**；回归建单 7/7。soroban 被跟踪的代码只多了「config+1 路由+1 按钮+api 一行」，抓取逻辑全在被 git 排除的 `scraper/soroban-scraper-taobao/` 里。
- **待办（需你账号+抓一次包）**：填 `session.py` 扫码登录判定、`fetch.py` 真实订单 mtop 接口、`normalize.py` 字段路径；填完按钮即通。

### 第三十四版：爬虫改插件架构（自动发现 + 插件管理页 + 定时/授权归 soroban）
把第三十三版的「单按钮触发」升级为**插件体系**（用户要「插件形式、soroban 自动加载、前端一页管理、授权/定时在 soroban、为将来 JD 铺路」；选型「子进程调标准 CLI」+「插件自存会话、配置存 soroban」）：
- **插件契约**：每个爬虫加 `plugin.toml` 清单（`id`/`name`/`version`/`python`/`entry`/`state_dir`/`params`）+ 标准 CLI（`login`/`fetch`/`status`/`demo`，**stdout 只输出一行 JSON**、日志走 stderr+文件保持 stdout 干净）。`soroban_client` 加 `token=` 支持——soroban 触发时下发**短期 JWT**（`create_access_token`），插件**免存 soroban 密码**；`.env` 账密仅脱离 soroban 手动跑时用。
- **soroban 后端**：新 `PluginConfig` 表（`plugin_id` 主键 / `enabled` / `params_json` / `schedule_minutes` / `last_run_at`）；新 `routers/plugins.py`：`discover()` 扫 `scraper/soroban-scraper-*` 读 `plugin.toml`（`tomllib`），`GET /api/plugins`（列插件+安装状态+每账号授权状态）、`PUT /{id}/config`、`POST /{id}/login`、`POST /{id}/fetch`（按账号下发 token 子进程触发）；`scheduler_loop` 每 60s 按各启用插件的 `schedule_minutes` 到点自动 fetch，放进 lifespan。**删掉**第三十三版的 `routers/scrape.py` 与 `SCRAPER_CMD/CWD`、全部订单页的「抓取」按钮（`config` 改留 `SCRAPER_DIR` 可选覆盖，默认 `soroban/scraper`）。
- **soroban 前端**：新「插件管理」页（侧栏+路由）：每插件卡片示安装状态、启用/定时开关、参数表单（按 manifest 渲染）、每账号授权状态 + 「授权登录/重新授权」「抓这个号」、「抓取全部账号」；`api` 换 `scrapeApi`→`pluginsApi`。
- **修占位 bug**：`session.py` 登录成功判定原用 glob `**taobao.com/**`——会匹配 `login.taobao.com` 本身而秒过（假成功、存下未登录的空会话）。改为谓词「跳离 login 子域才算成功」。
- 自测：`GET /api/plugins` 发现 taobao（installed=true）；`PUT config` 存账号/定时；`POST fetch` fire-and-forget 起子进程（`scrape.log` 见对应账号 fetch 记录）；`login` 端点起进程；未知插件→404、无 token→401；回归既有 8 接口全 200、前端 `vite build` 通过。新增平台爬虫 = 建目录+`plugin.toml`+实现同款 CLI，soroban 自动认到。

### 第三十五版：淘宝爬虫抓包实测落地（三 stub 填实）+ 闲鱼/平台勘探 + 全项目审查修复
用户账号 `c2` 实测（**全程无风控**）：拟真 iPhone 仿真打开 H5 订单页、拦 mtop 拿真实结构，把第三十四版的三处诚实 stub 填成可用实现。
- **接口实测**（记 `scraper/soroban-scraper-taobao/docs/接口与字段.md`）：订单列表接口 `mtop.taobao.order.queryboughtlistv2`（JSONP），订单在 `data.data` 下按 `<组件>_<订单号>` 聚合；`MainGroup.orderPrice`(分)=实付、`sellerInfo.status.text`=状态、`item.title/skuText/quantity`=商品/分类/数量、`Main.orderType=="fliggy"`=飞猪标记。分页=请求 `page` 递增（页面自带签名，滚动即触发）。
- **字段映射（按用户约定）**：`session.py`（跳离 login 页 **且** 有登录态 cookie 才**原子写**会话）、`fetch.py`（`parse_orders` 纯函数离线可测 + 拟人抓取 + 命中风控即停）、`normalize.py`：**「店铺」列改放商品标题**、**「物品」= 分类名(skuText)×数量**（前端 `itemSummary` 显示 `名称×数量`）；`order_date/express_no` 列表接口没有→留空人工补。前端把暂存/淘宝页 `shop` 列标题 **店铺→商品**。
- **闲鱼/平台区分报告**（`docs/闲鱼与平台区分.md`）：闲鱼订单**不在**淘宝订单列表里（只有「闲鱼转卖」按钮）→ 淘宝爬虫永不混入闲鱼；闲鱼是独立系统（`mtop.idle.web.trade.bought.list` / `www.goofish.com/bought`），且**淘宝会话搬不过去**（调其订单接口返回 `FAIL_SYS_SESSION_EXPIRED`、弹独立扫码登录），要抓需单独做 `soroban-scraper-xianyu` 插件。JD/PDD 按账号名区分（不加新功能）。
- **风控**：两次抓包 + 一次实机 fetch 均无风控；对策与分级停手预案记 `docs/风控与对策.md`。
- **全项目对抗审查（5 维度 → 逐条对抗验证，10 确认 0 误报）+ 全修**：
  - **(med) 会话原子写 + 授权校验**：`storage_state` 直写会中途崩溃留坏 JSON、`has_session` 只判存在 → 「显示已授权却永远抓不了」。改临时文件 `os.replace` 原子写；`has_session` 校验能 `json.load`。
  - **(med) 同账号抓取串行化**：定时/手动/CLI 并发会多开浏览器冲淘宝（风控红线）。加**跨进程文件锁** `<state_dir>/<account>.lock`，占用则抛 `AlreadyRunning` 软跳过。
  - **(med) 已导入行只更状态**：`soroban_client` 对已导入行、当状态映射为 None 时会误 patch 整个 body 覆盖账本人工改值。改为已导入行**只**发 `order_status`（无则跳过），绝不回写其它字段。
  - **(low×7)**：数量容错 `int(float(str(q)))`（防单个坏值拖垮整批）；分页**按业务页码去重**（原按含时间戳 URL 去重会漏页）；token 改走**环境变量 `SOROBAN_TOKEN`** 下发（不进 `ps`/argv）；定时 `last_run_at` 仅**成功启动才推进**（空账号/失败不推进，下轮重试）；删 `cmd_fetch` 里永不触发的 `NotImplementedError` 死分支+误导注释；删 `config.ACCOUNTS` 死代码；`_PUSH_FIELDS` 的 `fx_rate` 补前向兼容注释。
- 自测：离线解析 30 单字段全对；端到端回灌 15/20 单**幂等**（created→updated）；**实机 `python -m taobao_scraper fetch --account c2` 跑通**（真浏览器 ~50s、无风控、staging 落 20 单）；`_qty/_page_of/文件锁/已导入行 guard` 单测通过；后端 plugins.py 改后热重载、既有接口 200；前端 `vite build` 通过。个人数据（会话/抓包）全 gitignore。

### 第三十六版：淘宝爬虫改用桌面网页版(拿到下单日期) + 入库日期列 + 增量抓取 + 二次审查修复
按用户实测反馈调整（用户发现桌面版订单页直接显示下单日期/平台/订单号，H5 没有）：
- **数据源 H5 → 桌面版**：同一接口 `queryboughtlistv2`，桌面版 `buyertrade.taobao.com` 返回 **PC 模板**，比 H5 多带 **下单日期**(`shopInfo.createDay/createTime`)、`canViewLogistics`(能否看物流)、干净分页(`pageSize:30/hasMore/totalNum`)。实测无风控。`fetch.py` 重写：桌面上下文(非 iPhone)、新 `parse_orders`(PC 模板：shopInfo/orderStatus/orderPayment/orderItemInfo/orderExt 组件)、状态取 `tradeTitle`(交易态)为准(orderStatus.title 是退款/退货子态会误导)。`normalize` 现产出 `order_date`。
- **R1 入库日期列**：全部订单页「下单日期」列 → **入库日期**(`scraped_at`，只读，`#cell-scraped_at` slot + `fmtDate` 按 JST 显示)，方便按抓取批次筛选；下单日期仍存库、导入随行进账本。
- **R2 增量抓取**：`cmd_fetch` 先取 soroban 已有订单号(`existing_order_nos`)，翻页时**整页都是已有订单就停手**不再往下翻（配合一页 30 单，通常只抓 1 页，绝不深翻）。
- **R3 快递号**（待做）：只在详情/物流页、`canViewLogistics=true`(待收货)才有。计划：导入后定时任务**只对已导入且待收货的单**抓快递号（交易成功/关闭不抓）。当前账号无待收货单，需有一单再抓包定位字段。
- **平台区分/闲鱼**：用户暂缓（闲鱼数据链路当前不通），本版不采集平台标记。
- **二次对抗审查（2 agent）+ 修**：`cmd_fetch` 回灌段未包 try（token 过期/soroban 掉线会抛裸栈、soroban 收不到 JSON 结果行）→ 包住吐 `{ok:false}`；`unit_price` 未去「￥」→ 过 `_money`；翻页增量早停由「任一已见」改「整页已见」子集判定 + 点下一页后轮询新页真入列再读（防 expect_response 与 on_response 竞态）；首页响应改**轮询等待**(修 `pages[-1]` IndexError，实机首跑即命中已修)。
- 自测：离线解析桌面页 30 单(下单日期全有、unit_price 去符号、状态取交易态)；端到端回灌幂等(created→updated)；**实机 `fetch --account c2` 跑通**(真桌面浏览器、无风控、增量停在第 1 页、staging 30 单)；后端热重载 200、前端 `vite build` 通过。

### 第三十七版：全项目审查（5 维 / 27 确认）+ 部署更新文档 + 加列式自动迁移
用户要「整个项目过一次、修 bug、能否上线、全新机器怎么部署、以后 git 怎么更新」。5 维多智能体对抗审查（后端/安全/前端/运维部署/文档，27 条确认，1 high/7 med/19 low）。结论：后端金额/并发/软删/乐观锁/导入/看板/汇率**无真实 bug**、可判个人自用生产可用；缺口集中在**部署与文档**。已修：
- **(high 迁移) 加列式自动迁移**：`database.py` 去掉写死的 `_ADDED_COLUMNS`，改为启动时对比 model 与实际表、自动 `ALTER ADD` 缺失的**可空列**（非空无默认列只告警不动，需手动）。修掉历史踩过的坑（395abc8 加 fx_rate/order_status 未同步 → 旧库 git pull 后 `no such column`）；已用模拟旧库实测：缺列被自动补齐。
- **(med 生产托管) 后端同源托管前端**：`main.py` 检测到 `frontend/dist` 即 `StaticFiles` 挂载（挂最后，`/api/*` 优先）→ 生产只跑一个 uvicorn(不 --reload)、一个端口、无跨域；dev 无 dist 则跳过照旧用 vite。
- **(小修)** 插件下发 token 改真短期(30min)；`list_plugins` 的 `params_json` 解析加兜底防 500；`create_order` 加 `_check_shipment` 让挂靠不存在集运单走友好 422；`NotionTable` 列宽回退支持 `minWidth`；`start.sh` 校验 Python 3.11+；生成 `requirements.lock.txt`（锁定版本）；`.env.example` 补可选项注释；`docs` 里「故走 React」笔误改正。
- **文档**：根 `README.md` 基本重写——技术栈(Vue3/Element Plus，迁移写实)、目录结构(补 scraper/、去 bot/React)、君丰→集运、状态段更新，新增**「全新机器部署」「生产/长期运行」「更新(git)」「备份」「默认账号/改密码」**五章。
- **待用户拍板（未改）**：(1) 是否加「应用内改密码」接口（现只能首次用 `SOROBAN_ADMIN_PASS` 设、之后无改密入口）；(2) 是否开局域网(绑 0.0.0.0，开前必改密码)；(3) 是否引入 Alembic（当前加列式自动迁移够日常；结构性变更仍需手动+备份）。
- 自测：后端语法/热重载/既有接口全 200、加列迁移模拟实测、前端 `vite build` 通过；`.env`/`*.db`/`.state`/`backups` 均 gitignore、无敏感文件入库。

### 第三十八版：改密码接口/入口 + 端口避默认(8620/8621)
按用户拍板落地两项：
- **改密码**：新 `POST /api/auth/change-password`（校验原密码→hash 写回，需登录；原密码错→400、新密码<6 位→422）；前端侧栏底部加「改密码」弹窗（原/新/确认，失败给提示）。round-trip 实测通过（改→旧密码失效/新密码可登→错原密码 400→过短 422→复原 admin123）。补 `ChangePassword` schema、`authApi.changePassword`。
- **端口避开默认**：后端 8000→**8620**、前端 5173→**8621**（避与其它项目/常见默认冲突），且**环境变量可覆盖**（`BACKEND_PORT`/`FRONTEND_PORT`，start.sh export、vite.config 读、plugins `_SELF_URL` 读、config CORS 默认同步、插件 .env.example 同步）。单一真源，改一处全跟随。README 端口引用全部更新。
- ⚠️ 运行中的实例仍在旧端口，需**重跑 `./start.sh`** 才切到 8620/8621。

### 第三十九版：引入 Alembic 迁移 + 复审修复
用户要「加 Alembic + 再跑一次全项目审查」。
- **Alembic 落地**：`backend/alembic/`（env.py 用 SQLModel.metadata + `render_as_batch=True`）+ baseline 迁移 `53b7e33debd0`（autogenerate 后加了 `import sqlmodel`；已验证与 `create_all` 建的 schema **逐字节一致**，含 3 个 `deleted_at IS NULL` 部分唯一索引）。`database.py::create_db_and_tables()` 改为跑 `alembic upgrade head`，并对 **pre-Alembic 旧库自动 stamp 到 baseline 再升级**（无缝接管、不重建表、数据不动）。requirements 加 `alembic`。全场景实测：全新空库建全 13 表+admin、旧库接管保数据、`alembic check` 干净、CLI 通。
- **事故与恢复**：首轮复审有个 agent 擅自对**真实库**跑了 alembic 写命令，把 `alembic_version` 指向一个已删除的幻影版本 → 启动 `can't locate revision` 崩溃。已修：确认 schema 与 baseline 一致（数据完好 27 单/34 暂存）→ 重置 `alembic_version` 到真实 head → 启动恢复。并给复审加**只读铁律**（禁止对真实库/项目文件跑任何写/alembic 命令），重跑。
- **复审 6 条已修**：(med 回归) env.py `fileConfig` 默认 `disable_existing_loggers=True` 会在 app 内跑迁移后禁掉 uvicorn/soroban 全部 logger → 传 `disable_existing_loggers=False`；(low) env.py 无条件覆盖 url 使 database.py 传入 url 失效 → 改成仅 ini 占位时才覆盖；(low 回归) 改密码失败双弹提示（拦截器已弹）→ 去掉 catch 里的重复 `ElMessage`；(low doc) `.env.example` CORS 示例端口 5173→8621。
- **⚠️ 提交须知**：`backend/alembic/` 与 `backend/alembic.ini` 是新文件、必须 `git add` 一起提交，否则别的机器 clone/pull 后缺迁移脚本会启动失败。
- 自测：日志回归修复验证（迁移后各 logger `disabled=False`）、`alembic check` 干净、TestClient 启动 200、前端 `vite build` 通过、数据完好。

### 第四十版：数据库 SQLite↔MySQL 双向切换 + 记录连接过的库(一键切换) + 安全加固
把原本单向（SQLite→MySQL、切换即清空源库、不可逆）的数据库功能升级成**双向可逆**，并从 mercari_manage 借鉴「记录连接过的库」：
- **非破坏性切换**：`switch` 只改「连接指向」，不再清空源库（这正是原来单向、不可逆的根因）；`migrate` 改为**整表覆盖**目标（delete+copy，两方向通用），故切换可逆、可反复迁移。保留安全阀：不能切到「空/没迁过」的库。
- **记录连接过的库**：新增控制表 `db_connection`（本地 SQLite，DSN 用 Fernet 加密、host/user/db 明文仅展示），MySQL 目标在测试/迁移/切换成功时**自动记住** → UI 列表化，**一键切换无需重填密码**；本地 SQLite 恒在列表首位。端点 `/api/db/{status,test,migrate,switch}` + `DELETE /connections/{id}`。
- **安全**：所有 DSN 全程 Fernet 加密入库、密码永不回传前端、库名白名单防注入、全部端点需登录、异常里的 URL 由 SQLAlchemy 自动打码。
- **⚠️ 顺带修致命隐患**：`database.py::run_migrations` 判「pre-Alembic 旧库」时未排除新控制表 `db_connection` → **全新部署**会把它误判为业务表、错误 stamp 到 baseline 而**建不出业务表崩溃**（既有库因已有 `alembic_version` 未触发，只有全新装中招）。已把 `db_connection` 加入排除集。
- 自测：连接 CRUD/加密 round-trip（含 `@:/` 特殊字符密码）/去重/删除；**覆盖式拷贝两次幂等、不撞唯一约束**（证明切回可逆）、源↔目标逐表一致；全新 temp 库建表成功（部署 bug 已修）；6 维对抗审查确认「切回未复位暂存挂靠」等，已修。

### 第四十一版：全部订单加「来源」+ 插件按账号删订单(2 按钮) + 看板方言修复
- **来源(platform)**：`TaobaoStaging` 加 `platform`（淘宝/闲鱼/京东，与账本 `TaobaoOrder.platform` 对齐、导入随单迁移）；淘宝插件抓到的**默认「淘宝」**（`normalize` 打标 + `soroban_client` 下推白名单加 `platform`）；全部订单页加「来源」下拉列。迁移 `e5f6a7b8c9d0`。
- **插件按账号删订单**（用户要「2 个按钮分别删 2 个地方」）：插件页每账号加「删暂存单」（硬删该账号在全部订单页的暂存行+物品）与「删账本单」（软删该账号名下正式淘宝订单）。`tags.py` 加 `delete_account_staging`/`soft_delete_account_orders`。**对抗审查查出**软删账本没复位暂存挂靠 → 暂存永卡「已导入」指向已删单、无法重导 → 已对齐单条删除补上复位。
- **看板方言 bug（用户切到 MySQL 后暴露）**：`dashboard.py` 按月分组用了 SQLite 专用 `strftime()`，MySQL 报错。改为方言感知：MySQL 用 `DATE_FORMAT`、SQLite 用 `strftime`；全项目扫查确认仅此一处。顺带编了 5 条集运订单测试数据 + 挂靠 11 单淘宝订单。
- 自测：来源全链路（模型/schema/写读/导入随单迁移）；按账号删（暂存级联/账本软删/账号隔离/幂等）；看板在 MySQL 与 SQLite 双方言均 OK；集运列表/看板正常。

### 第四十二版：物品为最小单位（订单价由物品单价×数量派生）
为对接其它系统，把最小粒度从**订单**下沉到**物品**（详见上「数据模型 → 物品为最小单位」）。10 步实施、每步临时库实测：
- **模型**：`OrderItem`/`StagingItem` 加 `price_cny`(单价)+`auto`；`base.price_from_items()` + 各 model `sync_from_items()`（订单同时重算日元）。迁移 `f6a7b8c9d0e1` 加两列（`auto` NOT NULL DEFAULT 0，两方言通用，populated-table ALTER 实测）。
- **不变量**：每单/暂存 ≥1 物品；`routers/common.build_items()` 统一规整（无物品→自动 1 条；有物品无单价+有总价→折成第一件单价其余 0、标 auto；有单价→原样、无价者标 auto 灰显）。订单价 = Σ(单价×数量)、**只读**（update 忽略传入 price_cny）。
- **暂存↔账本对称**：导入随物品(含单价/auto)迁移；暂存页改物品写穿账本+镜像暂存；**淘宝页改物品也镜像回暂存行**（否则删单+重导会用陈旧暂存快照丢编辑——审查查出的 HIGH）。
- **回填**：`tools/backfill_item_price.py`（幂等）——41 单 0 物品→自动物品、含物品无价的→按总价折单价、标 auto 灰显待复核；数量4 的 6 单因单价取整总价 +¥0.02（已列出）。
- **前端**：淘宝/暂存两页人民币列改**只读**；物品展开面板加「单价」列；`auto` 物品**灰显**、编辑即转黑（`onItemEdit` 置 `auto=false`）。
- **爬虫**：无需改代码——它只送物品名/数量+订单总价，后端自动折单价、标 auto。
- **对抗审查 6 条**（6 维×逐条对抗验证）：修 5（2 HIGH：淘宝页改物品未镜像回暂存、爬虫重抓 seed=None 会把价清零；med：数量无 `ge=1` 下界→负价；low：回填对「NULL价+0物品」需 2 次收敛）；1 low（单价取整分位漂移）为单价口径固有、标 auto 复核。全 HTTP 端到端 + 回归通过。
- **生产已上线**：迁移(加列，非破坏)+回填已应用到生效 MySQL；迁移前存全量快照作安全网；不变量验证（0 空物品单、派生价 100% 等于存储价）；本地 SQLite 同步迁移+回填。⚠️ 需**重启后端 + 重建前端**才生效。

### 第四十三版：「淘宝订单」全量重命名为「商品订单」（Order）+ 账号字段 `platform_account`
来源已支持多平台（淘宝/闲鱼/京东/拼多多/其他）、最小单位是物品，"淘宝"这一命名已不贴切。展示名早改「商品订单」，本版把**表名/方法名/变量名/接口**一并对齐；`taobao_account`→`platform_account`（账号是各平台通用概念）。

命名对照（本文档以上所有 `淘宝/Taobao/taobao_*` 均为当时命名，现行代码一律用右侧）：

  | 旧 | 新 |
  |---|---|
  | 淘宝订单（显示） | 商品订单 |
  | `TaobaoOrder` / 表 `taobaoorder` | `Order` / 表 `orders`（类名撞 SQL 保留字 → `__tablename__="orders"`） |
  | `TaobaoStaging` / 表 `taobaostaging` | `OrderStaging` / 表 `orderstaging` |
  | `TaobaoStatus` / `TAOBAO_STATUS` | `OrderStatus` / `ORDER_STATUS` |
  | `TaobaoBase/Create/Update/Read/Brief` | `OrderBase/Create/Update/Read/Brief` |
  | `taobao_account`（列 / 接口字段 / 标签字段 / 爬虫推送） | `platform_account` |
  | `taobao_order_id`（orderitem 外键列） | `order_id` |
  | `imported_taobao_order_id`（暂存外键列） | `imported_order_id` |
  | `taobao_orders`（集运→订单关系） | `orders` |
  | 看板 `taobao_jpy` / `taobao_count` | `order_jpy` / `order_count` |
  | `/api/taobao`（+ `/shipment/{id}/taobao/{tb}`） | `/api/orders`（+ `/shipment/{id}/order/{order}`） |
  | `routers/taobao.py`、`models/taobao/` | `routers/orders.py`、`models/order/` |
  | 前端 `views/Taobao`、`taobaoApi`、路由/布局键 `taobao` | `views/Orders`、`ordersApi`、`orders` |

- **保留不改**（此处 "taobao" 确指淘宝平台/插件，而非订单概念）：来源值 `淘宝`；爬虫插件 id 与包名 `taobao`（`taobao_scraper` / `soroban-scraper-taobao` / `/plugins/taobao/...`）；`Source.taobao_bot`；OCR 的 `_TAOBAO_MARKERS`（识别淘宝/天猫截图）。
- **方式**：curated 整词替换（29 文件 ~380 处，按长度排序防子串误伤）+ 显式改路径/白名单 + `git mv` 三处文件/目录。
- **迁移 `b8c9d0e1f2a3`（跨方言）**：`RENAME TABLE/COLUMN`（MySQL 8+/SQLite 3.25+ 原生、引用它的外键自动跟随更新）；「活跃行唯一」约束（MySQL 生成列+唯一键 / SQLite 部分索引）改名前 `drop_active_unique`、改名后按新表名/新索引名 `emit_active_unique` 重建（生成列表达式只引用未改的 order_no/platform/is_delete）；普通索引 MySQL `RENAME INDEX`、SQLite `DROP+CREATE`；`columnlayout.table_name='taobao'→'orders'`、`tagoption.field='taobao_account'→'platform_account'` 两处**数据**用 `UPDATE` 迁移。含可往返 downgrade。
- **验证**：SQLite 副本 upgrade→downgrade→upgrade 往返全绿；MySQL 克隆库 `tset_soroban_migtest`（从 `tset_soroban` 复制 schema+数据）实测全绿后 DROP；本地 `soroban.db` 与生产 `tset_soroban` 均已应用——数据 49/49/30 不变，外键重指、MySQL 生成列重建、活跃唯一仍生效、看板求和/联表/账号筛选全对；改前存全量快照（SQLite 文件 + tset_soroban 行级 dump）作安全网。**另一套 MySQL 库 `soroban`（conn#2）按用户明确要求全程未动。** ⚠️ 需**重启后端 + 重建前端**才生效。

### 第四十四版：多智能体全项目审查（18 子系统 / 39→35 确认）+ 修复 25 处
只读审查（每条经独立对抗性复核去误报），按风险修复；未改任何数据库、未碰 MySQL `soroban`。
- **金额完整性**：`compute_money` 对**派生**货款/日元加上限校验（原先只卡直填列），越界抛 `ValueError`→`main` 全局转 **422**（新增 handler，与 IntegrityError→409 同风格）；`CNY_MAX/JPY_MAX` 提到 `config` 作唯一真相；物品 `quantity` 加 `le=1e6` 上限。防 Numeric(12,2)/有符号 INT 溢出（MySQL 报 500、SQLite 静默存超大数致双引擎发散）。
- **暂存↔账本一致**：删除**已导入**暂存行 → 409（否则孤立账本单、占死唯一号无法再导入）；`ignore` 加 `imported_order_id IS NULL` 门闸，已导入不可翻「已忽略」。
- **杂项汇率**：`create` 补当天汇率（对齐集运），修「填了人民币价没填汇率 → 静默结算成 0、不进看板」。
- **并发/TOCTOU**：`create_order` 的 `_check_shipment` 改标量 SELECT 直读 DB（原 `session.get` 命中身份映射缓存，flush 后复核形同虚设）；前端同订单 PATCH 经 `utils/orderWrites` 串行化，消除并发保存互撞 version 丢编辑；OCR 合并 409 改「拉新版本重算 patch 重试一次」而非直接丢弃。
- **插件账号名**：`login/fetch` 与 `add` 共用 `_check_account_name`（非空+无逗号+防目录穿越）；`rename_account` 提交后搬会话文件失败（历史非法 `old` 名）降级为警告，不再在改名成功后反报 4xx。
- **OCR**：成交价正则允许千分位逗号（`¥1,234.50` 原只取到 `1`）；`content_type` 缺失也按非图片拒绝。
- **标签**：`rename` strip `old`（原漏匹配→假 404）、存在性校验提到 `new==old` 短路前；`TagIn.value`/改色/改名 Query 加 `max_length=128`（防 MySQL 超长 500）。
- **迁移健壮**：`db_migrate.copy_data` 与 `scripts/migrate_sqlite_to_mysql` 改**单事务**（中途失败整体回滚，不留半拷贝目标让 `switch` 误接受）；`alembic env.py` 离线模式取生效 url（原死用 SQLite 控制库、对 MySQL 生成错方言 DDL）。
- **工具/爬虫**：`backfill_item_price` 种子改**货款口径**（总价−邮费），修 `sync` 重复计邮费致金额虚高（实测总价 110→110、不再 120）；爬虫 `upsert_orders` 仅**非空** items 才下推（空列表会被后端整体替换成占位、抹掉真实物品）。
- **前端杂项**：`bcrypt` 新密码限 72 字节（防静默截断）；`NotionTable` 仅 `minWidth` 的列拖动排序/拖宽不再被写成 160；杂项/集运删末行改 `load()`（原留空页）；`Shipment.loadUnassigned` 加 try/catch；`Plugins` 加/改账号 409 显式弹后端 detail（拦截器刻意跳过 409）；数据库表单按钮 busy 期间禁用；侧栏计算器空算式下不吞全局 Enter/Backspace；`fmtCNY/JPY` 非数字降级为「—」不显 NaN。
- **暂缓（记录待议，非本版改）**：改密码不失效历史 JWT（需给 User 加列+迁移，单用户低危）；登录计时枚举（单用户无实际价值）；爬虫 offset 翻页在并发插入下可能跳行（>500 行才触发，自愈）。
- **验证**：后端 `import`+mapper 全绿；TestClient E2E（溢出→422、杂项补汇率→结算 2400、删/忽略已导入→409）、`compute_money`/OCR 正则/密码 72 字节/backfill 不重复计邮费 单测全过；前端 `npm run build` 通过。⚠️ 需**重启后端 + 重建前端**才生效。

### 第四十五版：状态改名收尾 + 双引擎发散清零 + 列表接口去 N+1
承接第四十四版之后的一轮系统审计（详见 [审计报告-2026-08.md](审计报告-2026-08.md)）。新增 **372 条 pytest** 常驻回归（`backend/tests/`，跑临时库、不碰 `soroban.db`）。
- **⚠️ 最高危（数据静默丢失）**：`ceee9b7` 把 `交易成功` 改名 `已签收`，但爬虫 `normalize.STATUS_MAP` 仍映射旧值 → 淘宝上「交易成功/待评价」的订单回灌时撞枚举白名单 422，被 `upsert_orders` 计入 `skipped` **静默跳过**，日志只有数字变化。已修，并新增 `test_consistency.py` 把「后端枚举 ↔ 前端常量 ↔ 爬虫映射 ↔ 前端调用的 API 路径」这些**三处各写一份**的约定变成断言——这个 bug 就是它抓出来的。
- **OCR 补价失效且反馈失真**：`price_cny` 是派生列，`buildMergePatch` 直接 PATCH 它会被后端忽略，界面却报「已更新」。改为把成交价当**种子价** + 一份无单价物品一起发，由后端 `build_items` 折算；并加 `hasNoRealPrice()` 守卫，用户手填过价的单绝不覆盖。
- **31 个 VARCHAR 列无长度校验**（SQLite 静默存 / MySQL `Data too long` → 500）：新增 `schemas._len()` 以建表定义为唯一真相补齐校验，`weight`(Numeric(8,2)) 同理；`Create`/`Update` 里重复的字段声明收敛成 `OrderFieldsIn`/`ShipmentFieldsIn`/`MiscFieldsIn`。`test_lengths.py` 有元测试，加列漏加校验即红。
- **三个列表接口 N+1**：`ShipmentRead.model_validate()` 为填 `orders` 字段去懒加载关系（连已软删的子订单一起拉），随后又被过滤过的列表整个覆盖。改为 `_read_many()` 批量化 + `_shipment_read()` 只用本表列构造。`test_queries.py` 用 `before_cursor_execute` 数 SQL，守住「查询数不随行数增长」。
- **软删统一推进 version/updated_at**（原先只有批量路径推进）；`import_staging` 并发门闸补显式 `rollback`；`/api/tags/{field}` 的全表 DISTINCT 由两遍改一遍；`run_migrations` 的连接池泄漏。
- **`.requirements.sha256` 被误提交**：这是「本机装了哪版依赖」的指纹，一旦碰巧等于 `requirements.txt` 的哈希，新 clone 会**跳过 pip install**。已移除并 gitignore，`start.bat` 改把指纹放进 conda env 内（跟着它描述的环境走）。

### 第四十六版：拍板项落地 + 多智能体全项目审计（21 条确认 / 9 条证伪）
- **A. 商品标题列放宽为 TEXT**（迁移 `d0e1f2a3b4c5`）：`orders.shop`/`orderstaging.shop` 存的是商品标题，一单多件时爬虫拼成「A / B / C」天然超 255，而两列既不索引也不参与唯一约束——定长毫无意义。改 TEXT 后既不截断也不 500。SQLite 侧刻意不做 DDL（不强制长度，batch ALTER 反而要重建表、有搞坏部分唯一索引的风险）。
- **B+C. 种子价只认显式传来的 `price_cny`**，不再回退当前价。两条反直觉行为同源、一起消失：清空全部单价现在记 0+auto（**与只清空部分单价同口径**，旧行为是同一动作两种结果）；改邮费不再用「旧总价 − 新邮费」倒推货款。散在 6 处的换算收敛成 `common.goods_seed()`。
- **D. 单号写入即归一**：`express_no`/`intl_tracking_no` TRIM+UPPER（匹配键，且 SQLite 区分大小写而 MySQL 默认不区分 → 双引擎发散）；`order_no`/`shipment_no` 只 TRIM 不改大小写（有唯一索引，批量改可能撞约束让升级失败）。迁移一并归一历史数据。
- **⚠️ 最高危（认证绕过）**：打包出的 `soroban.exe` **从不生成 .env** → 用仓库里公开的默认 SECRET_KEY 签 JWT，叠加默认 `HOST=0.0.0.0`、自动播种 admin/admin123 并打印口令、登录无限速。同网段任何人自签一个令牌就是管理员。四层修复：`run.py` 首启生成随机 SECRET_KEY（**在 import app 之前**）、HOST 默认改环回、`main.py` 对不安全密钥 **fail-closed**（这类问题没法「登录进来再修」）、新增 `app/ratelimit.py` 登录失败指数退避。
- **打包链在任何新 clone 上都是断的**：`pyinstaller.bat` 调 `soroban.spec`，而标准 `.gitignore` 的 `*.spec` 把它整个忽略、从未进过版本库（与 `.requirements.sha256` 同类模板副作用）。已加 `!soroban.spec` 反排除并按代码里的 `sys._MEIPASS` 期望补写 spec。另修批处理 `%errorlevel%` 在括号块内**解析期展开**的陷阱——pip/npm 安装失败的三处检查从来不生效。
- **其余 13 条**：清空物品名会静默删物品（HIGH）；README 数据库整章过期，照做会以为切了 MySQL 其实还写 SQLite（HIGH）；按账号清空暂存硬删已导入行→孤儿账本单；Query 参数改名绕开长度校验；爬虫单价解析失效会把已有暂存价覆盖成 0；`requirements.lock.txt` 漏 3 个直接依赖导致「可复现安装」起不来；`backup.sh` 在 MySQL 模式静默备份过期 SQLite；MySQL 连不上时无自救出口（新增 `tools/use_local_db.py`）；集运/暂存新建 409 被吞；插件页时间早 9 小时；计算器全局吞 Enter 导致键盘点不动按钮；幽灵新建行每格建一条；列表页无请求序号防护。详见审计报告。
- **验证**：459 条 pytest 全绿；前端 `npm run build` 通过；真实起服务端到端复验（.env 生成、fail-closed、登录退避实测第 6 次起退避/第 7 次 429/换用户名不牵连）。⚠️ `soroban.spec` 与 `requirements.lock.txt` 未经真实构建/安装验证，需在 Windows 与干净 venv 上各跑一次。

### 第四十七版：命名歧义全部统一（列名/状态值/函数名）+ E/F 拍板
用户拍板「有歧义的都统一」。迁移 `e1f2a3b4c5d6` 一次性落地，含**三处数据迁移**（列改名、状态值改写、`columnlayout.columns_json` 里存的列键按表重映射），upgrade→downgrade→upgrade 往返实测通过。

| 旧 | 新 | 为什么 |
|---|---|---|
| `orders/orderstaging.shop` | `title` | 存的是**商品标题**（爬虫 normalize、UI 列头都叫「商品」），不是店铺名 |
| `orderitem/stagingitem.price_cny` | `unit_price_cny` | 与订单的 `price_cny`（**总价**）一名两义，且两者有 Σ单价×数量=总价 的关系，最易看串 |
| `orderstaging.status` | `import_status` | 一行两个 status：这个是**导入工作流**（待处理/已导入/已忽略） |
| `orderstaging.order_status` | `trade_status` | ……这个是**真实交易**状态，对齐上面 |
| `orders/shipmentorder/miscexpense.source` | `created_via` | 与 `platform`（UI 标签就叫「来源」）撞义；它其实是「这行怎么进来的」 |
| `OrderStatus.received = "已签收"` | `warehoused = "已入仓"` | 与集运单的「已签收」**同字面量不同义**（订单=国内快递被集运仓签收；集运=国际包裹本人收到），而 `EXCLUDED_STATUSES` 这类集合同时作用于两张表 |
| `ShipmentStatus.arrived` | `received` | 与 `OrderStatus.arrived("已到达")` 同属性名不同义 |
| `db_migrate.copy_data` | `replace_data` | 它会**先清空**目标库；`copy` 读不出破坏性 |
| `common.not_found/conflict` | `raise_not_found/raise_conflict` | 是 raise 而非 return，原名像构造器 |
| `plugins._find` | `_find_manifest` | 返回的是 plugin.toml 的解析结果 |

- **保留未改**：`OrderCreate/Update.price_cny`（在 create 上它就是「订单价」这个自然名；物品改成 `unit_price_cny` 后一名两义已消失，剩下的「输入是种子、输出是派生」有文档串说明）；`platform`（对 淘宝/闲鱼/京东 而言本就准确，歧义在 `source` 那一侧，已改）。
- **新增 `tests/test_naming.py`（16 条）**把每条约定写成断言：不许再出现 `shop` 列、物品价必须是 `unit_price_cny`、两个状态枚举的字面量必须**完全不相交**、前端无残留旧字段名、爬虫推送字段名跟得上。改回去就红。
- **F（改密码不失效 token）**：用户拍板**维持现状**，理由已写进 `auth.create_access_token` 的文档串（自用账本、共用者两人，为几乎用不到的「踢下线」加列+迁移+每请求多查一次不划算；真要强制下线改 SECRET_KEY 重启）。
- **E′（迁移/切换之间无写屏障）**：待定，方案见审计报告「二」。
- **验证**：475 条 pytest 全绿；前端 `npm run build` 通过；爬虫产出形状实测；真实起服务端到端复验（新字段名建单/暂存导入/看板/物品列表、单号归一、集运「已签收」与订单「已入仓」并存）。⚠️ 生产升级前请先 `./backup.sh`——本次迁移会改列名与状态值。

### 第四十八版：数据库迁移只读屏障 + 切换前变更检测（E′ 拍板落地）
用户定的方向：拷贝时锁库停一切写入；拷完解锁，之后改写就写旧库；切换那刻来不及的少量数据可接受。实施时补强了两点：

- **锁库是必需而非保险**：`replace_data` 逐表读源库，而 SQLite 侧**没有读快照**（pysqlite 只在写语句前发 BEGIN，SELECT 跑 autocommit）→ 拷贝中途的写入会产生**撕裂的拷贝**（订单拷了物品没拷、子表引用未拷贝的父行）。MySQL 源库反而一致（InnoDB 快照）——又一处双引擎发散。
- **「丢的只是爬虫能爬回来的」不成立**：还会丢 OCR 建的闲鱼订单、集运单、杂项、**所有编辑**（改价/改状态/挂靠/导入）、标签，而且**静默**。→ 加变更检测把静默丢失变成知情选择。

落地：
- **`app/maintenance.py` 只读屏障**，覆盖**三条写路径**（只拦 HTTP 不够）：① HTTP 写端点（`main.py` 中间件，`/api/db/*` 与登录放行——用中间件而非逐端点挂依赖，将来新增写端点自动覆盖）；② `fx_loop`（直接用 Session 写 FxRate，绕过 HTTP）；③ `scheduler_loop`（屏障期间别起爬虫子进程——白开一次浏览器冲淘宝，并发多开是风控红线）。
- **绝不泄漏**：try/finally + **硬超时自愈**（15min）。宁可迁移中途失去保护（最坏拷贝撕裂、可重来），也不能让账本永久只读。挂起时先等在飞写请求排空。
- **变更检测**：`migrate` 拷完记源库指纹（各表行数 + 最大 `updated_at`，存控制库新表 `migrate_state`，按目标分行）；`switch` 前重算比对，不同则 409 说人话「迁移之后当前库又有改动（商品订单 +3 条、集运订单 有改动）」。前端二选一：**重新迁移再切换**（默认）／仍然切换（放弃改动）。指纹刻意不含 `columnlayout`，拖列宽不该告警。
- **顺带修出真 bug**：加 `migrate_state` 后测试瞬间全红——`run_migrations` 判「pre-Alembic 旧库」的排除集是**手抄**的三张表名，新控制表没进去 → 全新部署被误判、stamp 到 baseline 而建不出业务表。改为从 `control_metadata.tables` **自动派生**，并加测试锁住。
- **F（改密码不失效 token）**：拍板维持现状，理由写进 `auth.create_access_token` 文档串。
- **验证**：501 条 pytest 全绿（新增 `test_maintenance.py` 26 条：写端点全被拦、读放行、`/api/db/*` 与登录放行、异常时屏障必撤、硬超时自愈、并发挂起被拒、等排空、指纹增删改检出、控制表排除集不许手抄）；前端 `npm run build` 通过；真实迁移 + 变更检测端到端复验。

### 第四十九版：SQL 方言专项审计 —— 排序规则根因修复 + 真 MySQL 契约测试
起因是一句「你再看 SQL 方言有没有明显的问题」。10 维度并行排查 + 双视角对抗证伪，33 条原始发现去重后 18 条全部存活。拿到真 MySQL（9.7）之后把所有推演逐条实测，结论收敛到**一个根因 + 若干独立缺陷**。

**根因：MySQL 侧所有字符串列落在 `utf8mb4_0900_ai_ci`，SQLite 是 BINARY。**
baseline 建表只给了 `mysql_charset='utf8mb4'` 而没给 COLLATE，MySQL 的规则是「声明了 CHARACTER SET 却没声明 COLLATE 时取**字符集**的默认排序规则」——不是库的，所以 `ensure_database` 里那句 `COLLATE utf8mb4_unicode_ci` 从来没生效过。实测 MySQL 9.7 判为相等的有：`'Alice'='alice'`、`'José'='Jose'`、`'ＡＢＣ'='ABC'`、`'ヤマダ'='やまだ'`、`'ｶﾞ'='ガ'`——对一个日本代购账本，命中面远不止「大小写」。

后果两条，都实测复现过：① SQLite 里合法共存的 `'Alice'/'alice'` 拷进 MySQL 撞 1062，而 `replace_data` 是单事务 → **整次迁移回滚**，且 UI 里无法自救（删标签说「正被使用」、改名说「新名字已被占用」）；② `routers/tags` 的按值批量改名/删除用 `WHERE col = value`，在 MySQL 上命中本不该动的行——实测「改一个收货人，另一个只差大小写的收货人被一起改掉」。

- **修法**：`dialect.BinStr(n)` 给「参与唯一约束」与「被批量精确匹配」的 11 个键列在 MySQL 上显式声明 `utf8mb4_0900_bin`，`emit_active_unique` 的三个生成列同样带上（唯一性是它说了算）。迁移 `f2a3b4c5d6e7`，顺序是**先拆生成列 → 再改基础列 → 最后重建**（生成列依赖基础列，否则 MySQL 拒绝改列）。SQLite 侧零 DDL。
- **为什么是 `utf8mb4_0900_bin` 而不是 `utf8mb4_bin`**：实测 `SHOW COLLATION` —— 后者是 **PAD SPACE**（`'a '='a'`），只有前者是 NO PAD，才与 SQLite BINARY 真正等价。`_0900_bin` 需 MySQL 8.0+（MariaDB 没有），故 `bin_collation()` 探测后回退。

**其余已修**：
- **`tools/backfill_item_price.py:38` 把 `unit_price_cny` 拼成 `unit_unit_price_cny`** —— SQLModel(table=True) 关掉了 Pydantic 校验，未知 kwarg 被**静默丢弃** → 单价落 NULL → `sync_from_items` 把总价重算成「只剩邮费」，**历史订单货款被清零**，二次运行还会把 0 固化。与方言无关，但优先级最高。已加事后校验（偏移超过按数量推导的取整容差就中止且不写库）+ AST 护栏扫全仓同类拼错。
- **只读屏障漏了第四条写路径**（第四十八版的遗留）：`GET /api/tags/{field}` 经 `_list→_sync` 会 INSERT/UPDATE `tagoption` 并 commit，中间件按「GET 是安全方法」放行 → 迁移拷贝期间打开任意带标签列的列表页就会往源库写。已让 `_sync` 自己查屏障（屏障期间只是不落库，下次 GET 自然补上），并加传递闭包扫描守住整类问题。
- **`OrderStaging.sync_from_items` 缺 `CNY_MAX` 卡口**：账本侧走 `compute_money` 顺带卡，暂存侧不调它。SQLite 静默落脏行并让**整个暂存列表 422 打不开**；MySQL 则 commit 时 1264 → 裸 500。抽出共用的 `guard_cny`。
- **`switch` 不检查目标库 schema 版本**：`migrate` 有 `run_migrations`，`switch` 没有 → 切到落后 schema 的库全站 500；更糟的是用户最自然的自救动作「迁移到此库」会用源库快照**覆盖**目标。改为切换前自动升级（排在 `is_target_empty` 之前，否则缺列异常会被兜底成「尚未建表」，正好把人引向那个会覆盖数据的按钮）。
- **时间列 `DATETIME(0)`**：实测存 `14:59:59.700000` 读回 `15:00:00`——MySQL 对小数秒是**四舍五入**且不报 warning，跨分钟跨小时，落在 UTC 日界附近还跨日。改 `UtcDateTime()` → `DATETIME(6)`（迁移 `a3b4c5d6e7f8`），SQLite 侧编译产物不变。
- **`orders._check_shipment` 的第二次复核在 MySQL 上是死代码**：InnoDB 默认 REPEATABLE READ，读视图在事务内第一次一致性读时就钉死（钉住它的往往是更早的鉴权 `session.get(User,…)`——FastAPI 的 per-request 依赖缓存让二者共用 Session），重读结果与第一次恒等。改成 `with_for_update()` 的 locking read；SQLAlchemy 的 sqlite 方言对 `FOR UPDATE` 是 no-op，SQLite 行为一字未变。
- **迁移前体检 `preflight`**：SQLite 不检查 VARCHAR 长度与 DECIMAL 范围，历史脏行会让拷贝在 MySQL 侧撞 1406/1264 而整批回滚。改为拷贝前扫出来、说清哪张表哪一行哪个字段，400 返回；拷贝失败也带上表名（`CopyFailed`，与 barrier 的 RuntimeError 分开，否则「拷贝失败」会被报成「有人在同时迁移」）。

**契约测试（本轮最值钱的产出）**：`tests/test_mysql_contract.py`，由 `SOROBAN_TEST_MYSQL_URL` 门控、默认跳过。它把**整个应用**的数据引擎临时切到 MySQL，再照常打 HTTP 端点——同一份路由代码、同一个请求，在另一个引擎上跑一遍比对可观测结果。此前几百条测试**全部跑在 SQLite 上**，本轮这一整类 bug 没有一条能被它们发现。

已验证这些测试不是装饰品：把库降级到修复前的 revision，四项断言（标签变体共存 / 改名不误伤 / 集运单号变体共存 / 微秒保留）**全部失败**，升级后全部通过。

**验证**：517 条 pytest（SQLite）+ 14 条契约测试（真 MySQL 9.7）= 531 全绿；前端 `npm run build` 通过；全部迁移在真 MySQL 上跑通 upgrade→downgrade→upgrade。

**仍需真机复验的**：本轮结论已基本全部实测，唯一未验的是 MariaDB（`dialect.is_mysql` 放行它，但它没有 `utf8mb4_0900_bin`，会走 `utf8mb4_bin` 回退分支——尾空格仍会折叠）。

### 第五十版：插件依赖一键安装 + 爬虫金额解析修复
起因是两句话：「前端要提示缺依赖之类的，如果缺就给一个按钮补」「你帮我修好爬虫」。

**先查清一件事：爬虫从来没成功回灌过。** 库里的硬证据——`pluginconfig` 0 行、`created_via='taobao_bot'` 的订单 0 条、带 `raw_json` 的暂存行 0 条；43 条订单里 42 条 `manual`、1 条 `imported`（源头是一条手打的测试暂存行）。所以问题不是「爬到的价格不对」，是这条链路一次都没跑通。拦路的是插件缺 `.venv`，`_launch` 直接 400。

**顺带澄清一个误解**：`.env` 从面板跑**根本用不到**。soroban 触发时通过 argv 下发 `--soroban-url`、通过环境变量下发 `SOROBAN_TOKEN`、账号走 `--account`，一概不读 `.env`（`.env.example` 第一行本来就写了「仅脱离 soroban 手动跑时才用」）。真正缺的只有 `.venv`。

**一键安装**：
- `POST /api/plugins/{id}/install`：建 venv → 装 `requirements.txt` → 按需下 Chromium。后台跑 + 轮询进度，绝不卡住 HTTP 请求（pip 拉包、浏览器 ~150MB 都是分钟级）。
- `GET /api/plugins` 新增 `needs`（缺什么，分 venv / deps / browser 三档，各带一句人话说明）与 `install`（running/step/error）。`installed` 的判据从「venv 文件在不在」改成「一样不缺」。
- **依赖探测必须缓存**：探测要在插件自己的解释器里逐个 `import`、再问一次 Playwright 浏览器路径，每次好几个子进程；而 `GET /api/plugins` 是前端轮询的接口，装依赖时还在秒级轮询。加了 60s TTL + 装完显式失效。
- **venv 判据是「解释器能跑」而不是「文件存在」**：venv 建到一半失败会留下 `bin/python` 符号链接，只看存在与否会把半成品当成装好了——前端于是不提示重建，却在下一步莫名报缺依赖。这个坑在本机真踩到了。
- **绕开 `ensurepip` 缺失**：Debian/Ubuntu 上「装了 python3 却没装 python3-venv」极常见，标准 `python -m venv` 会失败。先试标准方式（让 venv 自带 pip），失败才退到 `--without-pip`，再借 soroban 自己的 pip 用 `--python` 装进去（pip 23.1+）。用户不需要有 sudo。
- 前端：缺依赖时列出具体缺什么 + 「一键安装」按钮 + 「一并下载浏览器内核」复选框；安装中显示当前步骤，装完自动刷新解禁其它按钮；失败原因（pip/venv 的 stderr 尾巴）直接显示。

**爬虫金额解析的两处 bug，共同根源是「照着字段名猜，从没跟真实抓包核对」**——插件 `docs/接口与字段.md` §3 的字段映射表里**根本没有 `unit_price` 和 `postage_cny` 这两行**：

- **邮费永远丢失**：`pcPostFee` 的值形如 `含运费￥12.00`（`fetch.py` 自己的注释就这么写的），而 `_money` 只剥 `￥¥,`，剩下 `含运费12.00` → `normalize._postage` 的 `float()` 抛 ValueError → **静默返回 None**，全程 exit 0、日志一行 ok。改成正则抓第一个数字。
- **`actualTotalFee` 被当单价用**：名字里带 `Total`，按惯例是**商品行合计**，而 soroban 侧订单价 = `Σ(单价×数量)+邮费` → 数量 >1 时金额**放大 quantity 倍**。数量 = 1 时两种解释完全等价，所以猜错能长期不暴露。
  这一条**没有靠猜来修**：新增 `normalize.resolve_unit_prices`，拿订单实付 `actualFee`（由 `orderPayment` 组件独立解析，不会与 `priceInfo` 同时失效）当裁判，比较 `Σ(v×数量)` 与 `Σ(v)` 哪个更接近「实付 − 邮费」，判定结果记进日志（`unit`/`total`/`n/a`/`unsure`）。判不出来（折扣、赠品、字段真改了）就**降级**：丢掉单价、改推实付当种子价——保住「订单总额 = 实付」这个底线，明细可以事后补。判定分布本身就是健康指标：分布突变说明淘宝改口径了，比金额悄悄错掉更早暴露。

**爬虫补上了测试**：`pytest.ini` 指向的 `tests/` 目录此前**根本不存在**，插件零测试。新增 27 条纯解析层测试（离线可跑，不碰淘宝、不起浏览器）。已验证它们不是装饰品：把 `_money` 改回旧实现 → 5 条红；把单价裁决去掉 → 2 条红。

**验证**：后端 526 条（+14 条 MySQL 契约跳过）、爬虫 27 条、前端构建全绿；真实起后端跑插件 `demo` 命令走完整回灌链路（不碰淘宝），落库总价由物品单价正确派生。

**顺带修出一个影响「多久要重扫一次码」的问题**：`fetch` 用 `new_context(storage_state=…)` 读进会话，抓完 `b.close()` 走人——**从不回写**。而淘宝会在正常浏览过程中给 cookie 续期，这些续期全被丢弃，会话文件永远停在扫码那一刻、到期即死，**跟你用不用它无关**。新增 `session.refresh_state`：抓完把最新 cookie 原子回写，于是只要在过期前跑过一次抓取，有效期就往后滚——「扫一次」而不是「每月扫一次」。关键守卫是**只在仍为登录态时才写**：抓取中掉登录（风控/异地登录/改密码）时 ctx 里是一份未登录会话，写回去等于用坏会话覆盖掉还没到期的好会话，把「这次抓失败」升级成「以后都抓不了、且不知道为什么」。

**仍然做不了的**：这台机器无显示器，`session.py` 的扫码登录硬编码 `headless=False`，登录只能在有图形界面的机器上做。现有 `.state/c233.json` 的关键 cookie 有效期到 **2026-08-12**，过期后需去有屏幕的机器重新扫码再把会话文件拷回来。
