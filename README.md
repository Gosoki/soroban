# soroban（算盤）

个人代购／集运记账系统。追踪「淘宝下单 → 快递 → 集运到日本 → 杂项」的全流程开销，**统一按日元结算**，双币（人民币／日元）记账。

## 功能

- **看板**：总支出、按月趋势、各类占比（淘宝商品／集运运费／杂项）
- **淘宝订单**：可编辑表格，手动录入 + 加行 + 改行；列可拖动改序/改宽（持久化）
- **集运订单**：一个集运单关联多个淘宝订单（合包），展开可看/增删关联单
- **杂项支出**
- **双币结算**：填人民币按下单日期匹配当日汇率折算日元，可手动覆盖实付日元
- **全部订单（暂存区）**：待处理的淘宝订单，逐单「导入」进账本
- **爬虫插件**：`scraper/` 下的淘宝订单爬虫，soroban 自动发现、在「插件管理」页做授权/参数/定时（各插件独立 venv+Playwright，被 soroban git 排除）
- **登录**：多人共用一本账，登录状态长期保持（默认 90 天）

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Vue 3 + Element Plus + Vite + Axios |
| 后端 | FastAPI + SQLModel |
| 数据库 | **SQLite（WAL 模式）/ MySQL 可切换**——同一套代码按 `DATABASE_URL` 自动适配方言（见「数据库」章） |
| 迁移 | **Alembic**（启动自动 `upgrade head`；旧库首启自动接管；改 model 后 `alembic revision --autogenerate`，见「更新」章） |
| 汇率 | open.er-api.com（CNY→JPY，免费无 key） |

## 目录结构

```
soroban/
├── backend/                FastAPI + SQLModel
│   ├── app/
│   │   ├── config.py       配置（读 .env）
│   │   ├── database.py     engine（按方言构造）+ WAL（仅 SQLite）+ 启动跑 Alembic 迁移
│   │   ├── models/         数据模型（按页面功能解耦到子目录，见「数据库」章）
│   │   │   ├── base.py     共通基类/枚举/金额计算
│   │   │   ├── user/ taobao/ shipment/ misc/ fx/ config/   各页/各功能的表
│   │   │   └── __init__.py 统一 re-export（`from app.models import X` 保持不变）
│   │   ├── db/dialect.py   方言翻译层（SQLite 部分索引 ↔ MySQL 生成列）
│   │   ├── schemas.py      请求/响应模型
│   │   ├── auth.py         登录/JWT/密码哈希/改密码
│   │   ├── seed.py         建 admin（CLI）
│   │   ├── demo.py         灌演示数据（CLI）
│   │   ├── routers/        REST 接口（auth/taobao/shipment/misc/staging/dashboard/fx/layout/tags/plugins）
│   │   └── services/       汇率等
│   ├── alembic/            数据库迁移脚本（versions/ 里每个改动一个版本，需提交；迁移方言无关）
│   ├── alembic.ini         Alembic 配置
│   ├── scripts/            一次性脚本（migrate_sqlite_to_mysql.py：SQLite→MySQL 整库搬运）
│   ├── requirements.txt        直接依赖（宽松版本）
│   └── requirements.lock.txt   锁定版本（可复现安装）
├── frontend/               Vue 3 + Element Plus + Vite
├── scraper/                爬虫插件（各自成库/venv，soroban git 排除，仅留 README）
│   └── soroban-scraper-taobao/   淘宝订单爬虫（Playwright + H5/桌面 mtop 抓包）
├── docs/                   开发记录、设计决策、抓包实测记录
├── start.sh                一键启动（开发）
└── backup.sh               数据库备份（WAL 安全）
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

- **局域网从别的设备访问**：`start.sh` 默认后端只绑 `127.0.0.1`。要开放：后端起用 `--host 0.0.0.0`、前端 `npm run dev -- --host`（已含 `--host`），并把本机 IP 加进 `.env` 的 `CORS_ORIGINS`。⚠️ 开放前**务必改掉默认密码**（见下）。
- **爬虫插件（可选）**：soroban 只发现插件、不含其代码。要用淘宝爬虫，进各插件目录各自安装：
  ```bash
  cd scraper/soroban-scraper-taobao
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  .venv/bin/python -m playwright install chromium
  ```
  装好后在 soroban「插件管理」页即显示「已安装」，在那里扫码授权、设账号/定时。详见 `scraper/soroban-scraper-taobao/README.md`。

### 生产 / 长期运行（单进程、同源，无需 vite）

`start.sh` 是**开发模式**（`--reload` + vite dev server）。长期跑推荐构建前端、由后端同源托管：
```bash
cd frontend && npm run build          # 产出 frontend/dist
cd ../backend && BACKEND_PORT=8620 .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8620   # 不加 --reload
```
后端检测到 `frontend/dist` 会自动托管它（`/api/*` 走后端、其余回退到前端），于是**只需一个进程、一个端口(8620)**，前端相对 `/api` 天然同源、无跨域。可再用 macOS `launchd` / Linux `systemd` 做开机自启+崩溃重启。

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

同一套代码同时支持 **SQLite**（默认，零配置，适合单机）和 **MySQL**（多人/生产），由 `.env` 里的
`DATABASE_URL` 一个开关决定，无需改代码：

```bash
# 默认：SQLite 文件
DATABASE_URL=sqlite:///./soroban.db
# 切 MySQL（务必带 charset=utf8mb4；密码里的特殊字符要 URL 编码，如 @ → %40）
DATABASE_URL=mysql+pymysql://user:pass@127.0.0.1:3306/soroban?charset=utf8mb4
```

- **模型解耦**：表按页面功能拆在 `app/models/` 子目录（user/taobao/shipment/misc/fx/config），
  对外仍是 `from app.models import X` 扁平导入，业务代码无感。
- **方言差异集中翻译**：`app/db/dialect.py` 负责把 SQLite 与 MySQL 的语法差异统一。最典型的是
  「软删/空值感知的唯一约束」（订单号非空且未软删时才唯一）——SQLite 用**部分唯一索引**，
  MySQL 用**生成列 + 唯一键**等价实现，语义完全一致。
- **迁移方言无关**：`alembic upgrade head` 在两种库上都能建出正确 schema（启动时自动执行）。

### 从 SQLite 迁到 MySQL

```bash
# 1) MySQL 建库（utf8mb4）
mysql> CREATE DATABASE soroban CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

# 2) 装驱动（已在 requirements.txt）
pip install PyMySQL

# 3) 目标库建表：把 .env 的 DATABASE_URL 指向 MySQL，跑一次迁移
cd backend && python -m alembic upgrade head

# 4) 搬数据（按外键顺序整库拷贝；MySQL 生成列自动计算，不手写）
python -m scripts.migrate_sqlite_to_mysql --src sqlite:///./soroban.db
#   --dst 省略时用 .env 的 DATABASE_URL；目标表非空会中止（幂等保护）

# 5) 抽查 MySQL 数据无误后，保持 .env 指向 MySQL、重启后端即完成切换
```

要回退 SQLite，把 `DATABASE_URL` 改回 `sqlite:///./soroban.db` 重启即可（双方言并存）。

> 已在 MySQL 9.7 上实测：迁移、生成列、四种「软删唯一」语义、整库 ETL、应用启动路径全部通过。

## 备份

```bash
./backup.sh            # 用 sqlite3 .backup，WAL 安全；自动保留最近 30 份到 backups/
```
> `backup.sh` 仅针对 SQLite。用 MySQL 时改用 `mysqldump soroban > soroban_$(date +%F).sql`（或数据库自带的定时备份）。

建议挂定时（macOS 用 `launchd`，或 `crontab -e`）：
```
0 3 * * * /path/to/soroban/backup.sh >> /path/to/soroban/backups/backup.log 2>&1
```

## 默认账号 / 改密码

默认 `admin` / `admin123`。改密码两种方式：
- **应用内**：登录后，左下角侧栏点「改密码」，填原密码 + 新密码（≥6 位）即可。
- **命令行**：首次部署用 `SOROBAN_ADMIN_PASS='强密码' ./start.sh` 直接设定。

局域网/多人使用前请务必改掉默认密码。

## 状态

稳定迭代中（详见 [docs/README.md](docs/README.md) 的版本记录）。已完成：登录、看板、淘宝/集运/杂项三页、双币结算与汇率、全部订单暂存与导入、列布局持久化、淘宝爬虫插件（已可用）。
预留项：收入/利润（卖出侧打通）、导出 CSV/Excel、i18n。
</content>
