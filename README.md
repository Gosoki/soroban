# soroban（算盤）

个人代购／集运记账系统。追踪「淘宝下单 → 快递 → 集运到日本 → 杂项」的全流程开销，**统一按日元结算**，双币（人民币／日元）记账。

## 功能

- **看板**：总支出、按月趋势、各类占比（淘宝商品／集运运费／杂项）
- **商品订单**：可编辑表格，手动录入 + 加行 + 改行；列可拖动改序/改宽（持久化）
- **物品**：以「物品」为最小单位（一单多物，各带单价×数量），单独一页可按物品检索/改价
- **集运订单**：一个集运单关联多个商品订单（合包），展开可看/增删关联单；
  拖入「内含快递」截图即 OCR 自动关联
- **杂项支出**
- **双币结算**：填人民币按下单日期匹配当日汇率折算日元，可手动覆盖实付日元
- **日元汇率**：单独一页看汇率历史与来源；一天可有多条（每次抓取追加），手填的那条优先
- **暂存**：插件抓回来的待处理订单，逐单「导入」进账本
- **插件**：`plugins/soroban-plugin-*/` 下的外部数据插件（今天是淘宝订单爬虫与汇率），
  soroban 自动发现，在「插件」页做授权/参数/定时。**能力按 `plugin.toml` 声明授权**，
  核心默认拒绝（详见 `docs/README.md` 的插件章）
- **登录**：多人共用一本账，登录状态长期保持（默认 90 天）

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Vue 3 + Element Plus + Vite + Axios |
| 后端 | FastAPI + SQLModel |
| 数据库 | **SQLite（WAL 模式）/ MySQL 可运行期热切换**——同一套代码自动适配方言；切换入口在应用内「数据库」页，**不是** `.env`（见「数据库」章） |
| 迁移 | **Alembic**（启动自动 `upgrade head`；旧库首启自动接管；改 model 后 `alembic revision --autogenerate`，见「更新」章） |
| 汇率 | **由插件提供**（`plugins/soroban-plugin-fx`）。核心自己不抓汇率，只存与用；
  没装插件时可在设置页手填一条 |

## 目录结构

```
soroban/
├── backend/                FastAPI + SQLModel
│   ├── app/
│   │   ├── config.py       配置（读 .env）
│   │   ├── database.py     engine（按方言构造）+ WAL（仅 SQLite）+ 启动跑 Alembic 迁移
│   │   ├── models/         数据模型（按页面功能解耦到子目录，见「数据库」章）
│   │   │   ├── base.py     共通基类/枚举/金额计算
│   │   │   ├── user/ order/ shipment/ misc/ fx/ config/   各页/各功能的表
│   │   │   └── __init__.py 统一 re-export（`from app.models import X` 保持不变）
│   │   ├── db/dialect.py   方言翻译层（SQLite 部分索引 ↔ MySQL 生成列）
│   │   ├── schemas.py      请求/响应模型
│   │   ├── auth.py         登录/JWT/密码哈希/改密码
│   │   ├── seed.py         建 admin（CLI）
│   │   ├── demo.py         灌演示数据（CLI）
│   │   ├── routers/        REST 接口（auth/orders/items/shipment/misc/staging/dashboard/
│   │   │                   fx/layout/tags/plugins/dbadmin/settings/meta/ingest）
│   │   ├── services/       汇率、OCR、插件写入通道（ingest）
│   │   └── plugins/        插件清单解析与权限（manifest/scopes/params）
│   ├── alembic/            数据库迁移脚本（versions/ 里每个改动一个版本，需提交；迁移方言无关）
│   ├── alembic.ini         Alembic 配置
│   ├── scripts/            历史脚本（migrate_sqlite_to_mysql.py：已被「数据库」页取代）
│   ├── tools/              一次性/自救脚本（use_local_db.py：MySQL 连不上时切回本地）
│   ├── tests/              pytest（跑临时库，不碰 soroban.db；见 tests/README.md）
│   ├── run.py              打包入口（首启生成含随机 SECRET_KEY 的 .env）
│   ├── requirements.txt        直接依赖（宽松版本）
│   └── requirements.lock.txt   锁定版本（可复现安装）
├── frontend/               Vue 3 + Element Plus + Vite
├── plugins/                插件（各自成库/venv，soroban git 排除，仅留 README）
│   ├── soroban-plugin-taobao/    淘宝订单爬虫（Playwright + H5/桌面 mtop 抓包）
│   └── soroban-plugin-fx/        汇率（核心已不含抓取逻辑）
├── docs/                   开发记录、设计决策、抓包实测记录、审计报告
├── start.sh                一键启动（开发）
├── pyinstaller.bat         打包 soroban.exe
├── soroban.spec            PyInstaller 清单（手写，**必须提交**——标准 .gitignore 的 *.spec 会误伤）
└── backup.sh               数据库备份（WAL 安全；MySQL 模式下会拒绝执行）
```

## 本地运行

**一键启动（推荐）**：
```bash
./start.sh
```
首次运行自动建 venv、装前后端依赖、生成 `backend/.env`（随机 SECRET_KEY）、建 admin；之后同时起后端(8620)+前端(8621)。浏览器开 http://localhost:8621 （默认 `admin` / `admin123`），Ctrl+C 一起停。
> 端口特意避开常见默认（8000/5173），防与其它项目冲突；要改用环境变量：`BACKEND_PORT=9620 FRONTEND_PORT=9621 ./start.sh`（前后端会自动保持一致）。

<details><summary>手动分开跑</summary>

后端：
```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate   # 需 Python 3.11 或 3.12（3.13 暂不支持，见下）
pip install -r requirements.txt                        # 或 requirements.lock.txt（锁定版本）
cp .env.example .env
python -c "import secrets;print(secrets.token_hex(32))" # 把输出填进 .env 的 SECRET_KEY=
python -m app.seed                                     # 建 admin（默认 admin/admin123）
uvicorn app.main:app --reload --port 8620
```
前端（另开终端）：
```bash
cd frontend
npm install
npm run dev                   # http://localhost:8621 （代理 /api → :8620）
```
</details>

## 全新机器部署

**前置**：`git`、**Python 3.11 或 3.12**、`node`/`npm`。
> 版本范围：下限 3.11（插件发现用标准库 `tomllib`）；**上限 3.12——3.13 暂不支持**：OCR 依赖 `rapidocr_onnxruntime`→`onnxruntime`/`numpy` 在 3.13 上常无预编译 wheel，装依赖会失败。`start.sh` 会自动优先挑 `python3.12`/`python3.11` 建 venv，挑不到会明确报错。

```bash
git clone https://github.com/Gosoki/soroban.git
cd soroban
SOROBAN_ADMIN_PASS='你的强密码' ./start.sh     # 首次即设定管理员密码（不设则默认 admin123）
```
`start.sh` 会自动：建 venv、装依赖、生成含随机 SECRET_KEY 的 `.env`、建 admin、装前端依赖、起服务。浏览器开 http://localhost:8621 。

- **局域网从别的设备访问**：前后端共用同一个 `HOST` 旋钮，默认两边都只绑环回。要开放：

  ```bash
  HOST=0.0.0.0 ./start.sh          # Windows: set HOST=0.0.0.0 && start.bat
  ```

  ⚠️ 开放前**务必改掉默认密码**（见下）。不要只给前端加 `--host`：dev server 把 `/api` 反代到后端，前端单边对外等于后端也一起对外，而后端那句 `--host 127.0.0.1` 看着还在、其实已经不设防。同源代理下 `CORS_ORIGINS` 不起作用，不用配。
- **插件（可选，但汇率靠它）**：soroban 只发现插件、不含其代码。把插件目录放进 `plugins/` 之后，
  打开「插件」页——缺依赖会直接列出缺什么（Python 环境 / Python 依赖 / 浏览器内核），
  点**「一键安装」**即可，装完按钮自动解禁，不用开终端。

  soroban 会用自己的解释器建插件的 `.venv`、装 `requirements.txt`、按需下载 Chromium。
  本机 `ensurepip` 不可用时（Debian/Ubuntu 上「装了 python3 却没装 python3-venv」很常见）
  会自动改用 `--without-pip` 建，再借 soroban 的 pip 装进去——不需要 sudo、不需要先 apt。

  也可以手动装（想脱离面板单独用命令行时）：
  ```bash
  cd plugins/soroban-plugin-taobao
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  .venv/bin/python -m playwright install chromium
  ```
  装好后在「插件」页勾选授权、设账号/定时。详见 `plugins/soroban-plugin-taobao/README.md`。

  ⚠️ **汇率现在也是插件**（`plugins/soroban-plugin-fx`）：核心不再自带任何抓取逻辑。
  不装它就没有自动汇率，只能在设置页手填一条；侧栏会把「手填」和「已过期」都标出来。

  > ⚠️ **扫码登录必须有图形界面**（`session.py` 硬编码有头浏览器）。无头服务器上装得了、抓得了，
  > 但授权那一步得在有屏幕的机器上做，再把 `.state/<账号>.json` 拷过去。

### 生产 / 长期运行（单进程、同源，无需 vite）

`start.sh` 是**开发模式**（`--reload` + vite dev server）。长期跑推荐构建前端、由后端同源托管：
```bash
cd frontend && npm run build          # 产出 frontend/dist
cd ../backend && BACKEND_PORT=8620 .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8620   # 不加 --reload
```
后端检测到 `frontend/dist` 会自动托管它（`/api/*` 走后端、其余交给静态目录），于是**只需一个进程、一个端口(8620)**，前端相对 `/api` 天然同源、无跨域。可再用 macOS `launchd` / Linux `systemd` 做开机自启+崩溃重启。
> 路由用的是 **hash 模式**（`/#/orders`），所以浏览器只会向后端请求 `/`，不需要 SPA history 回退。
> 若将来改成 history 模式，得给后端补一条「未知路径返回 index.html」的兜底，否则刷新子页面会 404。

> ⚠️ **不要加 `--workers`（也别在 gunicorn 后面挂多个 worker）。** soroban 只能单进程跑：
> 插件令牌的撤销表、在飞子进程表、批次聚合、安装进度全都是**进程内**状态。多开一个进程，
> 插件回灌会被负载均衡分到没有那枚令牌的进程 → **全线 401**，表现是「抓了一批单一条都没回来」，
> 而日志里只有一串 401，看不出跟 worker 数有关。
> 现在这是有**闸门**的：同一份数据目录的第二个进程会拿不到 `soroban.lock` 并当场退出并说明原因
> （`app/single_process.py`）。同机跑两份互不相干的账本仍然可以——换目录 + 换端口即可。
> 并发能力不是瓶颈：单人记账的负载，一个进程绰绰有余。

## 更新（git）

```bash
./backup.sh            # 1) 先备份数据库（重要）
git pull               # 2) 拉最新（在 master 分支）
# 3) 依赖有变才重装：
#    后端 backend/requirements*.txt 有变 → 进 backend 重新 pip install
#    前端 package.json 有变 → cd frontend && npm install（生产别忘 npm run build）
# 4) 重启服务（重新跑 ./start.sh，或重启你的 uvicorn/systemd）
```

**数据库结构变更怎么办**：用 **Alembic**，启动时自动 `upgrade head`（幂等）——
- 你只管 `git pull` + 重启：若更新带了新的迁移脚本（`backend/alembic/versions/*.py`），启动时**自动应用**。✅
- **旧库（Alembic 之前建的）首次启动会自动接管**（stamp 到 baseline 再升级），不用手动处理、数据不动。✅
- **改了数据模型（开发者）**：`cd backend && alembic revision --autogenerate -m "说明"` 生成迁移脚本、检查后提交；用户下次 pull+重启即自动升级。
> 你的 `.db` 与 `backups/` 都被 gitignore，`git pull` 不会动到数据。升级前仍建议先 `./backup.sh`。

## 数据库（SQLite / MySQL）

同一套代码同时支持 **SQLite**（默认，零配置，适合单机）和 **MySQL**（多人/生产）。

> ⚠️ **切换入口是应用内的「数据库」页，不是 `.env`。**
> 早期版本靠改 `.env` 的 `DATABASE_URL` 切库，现在**不是了**：`DATABASE_URL` 只用来定位那个
> 恒为 SQLite 的**控制库**（`backend/app/database.py::_control_url`——填 MySQL 串会被直接忽略、
> 回退到 `sqlite:///./soroban.db`）。往里填 MySQL 串不会报错、也不会生效，
> 你以为切过去了，实际所有新数据仍然写在本地 SQLite 里。

**两个库，各司其职**：

| | 存什么 | 在哪 |
|---|---|---|
| **控制库** | 「当前用哪个后端」+ 加密的 MySQL 连接串（表 `app_db_config` / `db_connection`） | 恒为本地 SQLite（`DATABASE_URL` 指的就是它） |
| **数据库** | 全部业务数据（订单/集运/杂项/暂存…） | SQLite 或 MySQL，由控制库里的配置决定，可**运行期热切换**、无需重启 |

- **模型解耦**：表按页面功能拆在 `app/models/` 子目录（user/order/shipment/misc/fx/config），
  对外仍是 `from app.models import X` 扁平导入，业务代码无感。
- **方言差异集中翻译**：`app/db/dialect.py` 负责把 SQLite 与 MySQL 的语法差异统一。最典型的是
  「软删/空值感知的唯一约束」（订单号非空且未软删时才唯一）——SQLite 用**部分唯一索引**，
  MySQL 用**生成列 + 唯一键**等价实现，语义完全一致。
- **迁移方言无关**：`alembic upgrade head` 在两种库上都能建出正确 schema（启动时自动执行）。

### 迁到 MySQL（全程在网页上点）

打开左侧「**数据库**」页：

1. **连接新的 MySQL**：填主机/端口/用户名/密码/库名 → 点「测试连接并记住」。
   库还不存在也没关系，迁移时会自动建（utf8mb4）——但账号需要 `CREATE DATABASE` 权限。
2. 点「**迁移到此库**」：建库 → 建表（Alembic）→ 把**当前库**的数据整表覆盖过去。此步**不切换**、不动当前库。
   > 拷贝期间**全站只读**：所有写操作返回 503，汇率刷新与定时抓取一并暂停。
   > 这不是保险而是必需——拷贝逐表读源库、SQLite 侧没有读快照，期间的写入会产生
   > 「订单拷了、物品没拷」这种撕裂的副本。通常一两秒就结束。
3. 抽查 MySQL 里的数据无误后，点「**切换**」：热切换，无需重启。
   > 若这中间你又录过数据，切换会被**拦下来**并告诉你差了什么（如「商品订单 +3 条」）——
   > 那些改动不在目标库里，切过去就没了。可以选「重新迁移再切换」（推荐）或明确放弃它们。

连接串（含密码）用 `SECRET_KEY` 派生的 Fernet 密钥加密后存在控制库里，**永不回传前端**。

**回退**：同一页选「本地 SQLite」那一行点「切换」即可。切换是非破坏性的（不清空任何库），
所以可以来回切；但本地那份数据停在你当初迁走的那一刻——**要拿它当最新账本，得先「迁移到此库」**。

> ⚠️ **服务起不来时怎么切回本地**：数据在 MySQL 上而 MySQL 连不上时，soroban 会**拒绝启动**
> （刻意不自动降级——本地那份是旧数据，对着它记账会造成两边各有一半）。此时网页点不到，用命令行：
> ```bash
> cd backend && .venv/bin/python -m tools.use_local_db
> ```
> 它只改「当前用哪个后端」这一个标记，不动任何数据。

> `backend/scripts/migrate_sqlite_to_mysql.py` 是「数据库」页出现之前的一次性脚本，
> 功能已被上面的流程取代，保留仅供命令行批处理场景。

> 已在 MySQL 9.7 上实测：迁移、生成列、四种「软删唯一」语义、整库 ETL、应用启动路径全部通过。

## 备份

```bash
./backup.sh            # 用 sqlite3 .backup，WAL 安全；自动保留最近 30 份到 backups/
```
> `backup.sh` 仅针对 SQLite。它会**先读控制库确认当前后端**：若已切到 MySQL 就直接报错退出
> （不再静默备出一份「体积正常、表齐全、却停在迁移当天」的旧快照——那种备份挂 cron 天天成功、
> 真出事去恢复才发现丢了几个月）。用 MySQL 时请改用：
> ```bash
> mysqldump --single-transaction --default-character-set=utf8mb4 soroban > soroban_$(date +%F).sql
> ```

建议挂定时（macOS 用 `launchd`，或 `crontab -e`）：
```
0 3 * * * /path/to/soroban/backup.sh >> /path/to/soroban/backups/backup.log 2>&1
```

## 默认账号 / 改密码

默认 `admin` / `admin123`。改密码两种方式：
- **应用内**：登录后，左下角侧栏点「改密码」，填原密码 + 新密码（≥6 位）即可。
- **命令行**：首次部署用 `SOROBAN_ADMIN_PASS='强密码' ./start.sh` 直接设定。

局域网/多人使用前请务必改掉默认密码。

## 测试

```bash
cd backend
.venv/bin/python -m pip install pytest    # 只需一次（pytest 不在 requirements.txt 里）
.venv/bin/python -m pytest
```

跑在**临时库**上（`conftest.py` 在导入 app 前把 `DATABASE_URL` 指到临时目录，并把
`PLUGIN_DIR` 与兼容别名 `SCRAPER_DIR` **两个都**指到空目录），不碰 `soroban.db`、
也不会启动真实插件。两个都要设：只设旧名的话现名会回落到仓库里的 `plugins/`，
测试就变成「取决于本机装了哪些插件」。覆盖范围与各文件职责见
[backend/tests/README.md](backend/tests/README.md)。

其中 `test_consistency.py` 值得单独一提：订单状态枚举、状态生命周期序、爬虫的状态映射、
列布局白名单、前端写死的 API 路径——这些「三处各写一份、只能靠约定同步」的常量被写成了断言，
改后端忘了改前端/爬虫就会红。

### 双引擎契约测试（可选，需要一个真 MySQL）

上面那些测试**全部跑在 SQLite 上**，所以「SQLite 全绿、切到 MySQL 才炸」的那一类 bug
它们天然看不见（排序规则、`DATETIME` 精度、`DECIMAL` 范围、`INSERT IGNORE` 吞截断……）。
`test_mysql_contract.py` 补这一层：它把**整个应用**的数据引擎临时切到 MySQL，再照常打
HTTP 端点，比对可观测结果。没给连接串就自动跳过。

```bash
cd backend
SOROBAN_TEST_MYSQL_URL='mysql+pymysql://user:pass@host:3306/db?charset=utf8mb4' \
  .venv/bin/python -m pytest
```

⚠️ 它会**清空**目标库的业务表，只能指向专用测试库，别指向在用的库。

**必须是 MySQL 8.0 或更新版本，MariaDB 不支持。** 两条硬依赖：
键列要用 `utf8mb4_0900_bin` 才能与 SQLite 的逐字节比较等价（`utf8mb4_bin` 是 PAD SPACE，
尾空格会被折叠），而它是 MySQL 8.0 引入的；迁移链里还用到了 8.0 才有的 `RENAME COLUMN`。

> 这里曾承诺老服务端会**自动降级**到 `utf8mb4_bin` 且行为一致——**那是假的**。
> 降级逻辑确实存在（`dialect.bin_collation()`），但建表走的 `dialect.BinStr` 硬写了
> `utf8mb4_0900_bin`，绕过了它。照着那句话去连老服务端，迁移会在中途 `ERROR 1273`，
> 而 MySQL 的 DDL 是隐式提交的 → 库停在半升级态。
> 现在「数据库」页的**测试连接**与**迁移**两处都会先查版本并当场拒绝，
> 把「炸在中途」换成「一开始就说清楚」。

## 状态

稳定迭代中（详见 [docs/README.md](docs/README.md) 的版本记录）。已完成：登录、看板、商品/物品/集运/杂项四页、双币结算与汇率（汇率页）、暂存与导入、
列布局持久化、截图 OCR 录单、按能力授权的插件机制（淘宝爬虫与汇率两个插件均可用）。
预留项：收入/利润（卖出侧打通）、导出 CSV/Excel、i18n。
</content>
