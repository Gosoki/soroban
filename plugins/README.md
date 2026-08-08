# soroban 插件

soroban 扫本目录下的 `soroban-plugin-*`（各含 `plugin.toml`）作为插件。
插件是**独立仓库、独立环境**，soroban 不跟踪其代码，只按清单调它的标准 CLI。

**插件不等于爬虫**。淘宝订单抓取只是第一个；汇率、国际快递查询都没有「爬」的语义。
所以目录、前缀、术语一律叫 plugin。（旧的 `scraper/soroban-scraper-*` 仍会被扫描，
是为了老部署升级后插件不至于凭空消失，不是长期形态。）

## 现有插件

| 目录 | 干什么 | 要装吗 |
|---|---|---|
| `soroban-plugin-taobao` | 抓淘宝/闲鱼订单 → 暂存表 | 要（Playwright + Chromium，约 400MB） |
| `soroban-plugin-fx` | 取汇率 → 账本 | **不用装**（只依赖 httpx，跑 soroban 自己的解释器） |

## 两条通道，仅此两条

```
核心 → 插件    子进程 CLI。令牌走 SOROBAN_TOKEN、配置走 SOROBAN_CONFIG，
               都不进 argv（进程表 ps 与日志里看得见 argv）。
插件 → 核心    HTTP，带**限权**令牌。要么用现成的 REST（如淘宝插件写 /api/staging），
               要么用通用写入通道 POST /api/plugins/ingest。
```

**加一种新数据不需要新开接口**：写一个 handler 并 `@register(...)`（见
`backend/app/services/ingest/kinds/`），插件发 `{"kind": "...", "items": [...]}` 即可。
需要自己存点东西（轨迹去重、上次轮询时间）用 `kind = "plugin.record"`，按插件隔离命名空间。

## 权限：默认全拒

清单里 `scopes = [...]` 声明要什么，**用户在插件页勾选后才真正生效**。
实际拿到的是「清单声明 ∩ 用户授权 ∩ 核心已知」的交集，任务结束令牌立即作废。

三重交集各挡一件事：多勾没用（插件自己没声明）、插件升级偷偷加一项不生效（卡片标
「需要新授权」，`git pull` 不该悄悄扩权）、核心删掉某个权限后旧授权自动失效。

### 这套东西防的不是恶意插件

插件是本机子进程、代码你自己能改——拿到令牌就能干令牌能干的事。它防的是：

1. **误伤**：插件里一个写错的 URL 把 DELETE 发到了 `/api/orders/1`；
2. **静默扩权**：更新插件后它多要了一项权限，没人察觉；
3. **说不清**：出问题时无法回答「那一轮插件到底动了什么」。

对你可见的价值只有一句：卡片上写着「本插件只能写暂存，不能直接进账本」，**而这句话是真的**。
真话的前提是权限挂在**路由**上而不是路径前缀上——`POST /api/staging/{id}/import` 在
`/api/staging` 前缀下，干的却是「直接建正式账本单」。

## 写一个插件

```toml
id = "myplugin"
name = "我的插件"
version = "0.1.0"

python = "inherit"          # 依赖已在 soroban 里 → 不建 venv、不用安装
# python = ".venv/bin/python"   # 有重依赖就走独立环境

entry = "-m my_plugin"      # 标准 CLI
scopes = ["fx:write"]       # 要什么权限（用户在卡片上勾选后才生效）
settings = ["fx.stale_hours"]  # 要读核心的哪几项设置（**跨插件通用的偏好**才放核心；
                               # 只有本插件懂的用下面的 [[params]]）

# 按账号展开成多个子进程；不写 = 整体跑一次（汇率、快递查询这类）
# accounts = true
# accounts_ledger_field = "platform_account"   # 账号名落到账本哪一列（才支持按账号改名/删单）

# 能执行什么。**卡片上的按钮就是按这些长出来的**，核心不认识任何具体动词。
[[commands]]
name = "fetch"              # 传给你 CLI 的动词
label = "抓取"              # 按钮上的字
hint = "鼠标悬停时的说明"
primary = true              # 主按钮（高亮那个）
needs = ["fx:write"]        # 缺权限就禁用按钮并说明，而不是点了收 403
# per = "account"           # 每个账号起一个进程；默认 plugin = 整体一次
# confirm = "确定要清空吗？"  # 非空则先弹确认

# 插件私有参数。**卡片上的表单就是按这些长出来的**，核心不理解其含义，
# 只负责存、校验类型、渲染控件、下发（运行时在 SOROBAN_CONFIG 的 params 里）。
[[params]]
key = "timeout"
label = "超时（秒）"
type = "int"                # bool | int | str | select | secret
default = 20
min = 5
max = 120
hint = "?" 号里的说明
```

**跨插件通用的偏好放核心**（`services/prefs.SPECS`，设置页统一渲染），插件用 `settings = [...]`
声明要读哪几项；**只有本插件懂的**放 `[[params]]`。分不清就问一句：换个插件还成立吗？

`secret` 类型的参数存下来但**不回显**（API 只回「已设置/未设置」），日志里也会脱敏。

CLI 约定：**stdout 只吐一行 JSON**（soroban 解析它写日志），日志走 stderr，
有失败就非零退出——否则「30 单全被拒」和「一切正常」在界面上长得一模一样。
