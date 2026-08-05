# 测试

```bash
cd backend
.venv/bin/python -m pip install pytest      # 只需一次（pytest 不在 requirements.txt 里）
.venv/bin/python -m pytest                  # 全量
.venv/bin/python -m pytest tests/test_orders.py -v
.venv/bin/python -m pytest -k postage       # 按名字挑
```

**不碰你的库**：`conftest.py` 在 import `app.*` **之前**把 `DATABASE_URL` 指到一个临时目录
（环境变量优先于 `.env`），并把 `SCRAPER_DIR` 指到空目录，所以既不会动 `soroban.db`，
也不会发现/启动真实爬虫插件。不需要 `.env`。

## 各文件负责什么

| 文件 | 覆盖 |
|---|---|
| `test_auth.py` | 登录、JWT（伪造/过期/换密钥/缺 sub）、改密码、全路由鉴权 |
| `test_money.py` | `compute_money` / `price_from_items`：四舍五入、覆盖优先、特殊费、溢出 |
| `test_orders.py` | 商品订单 CRUD、物品派生价、邮费、乐观锁、软删、`(订单号,来源)` 唯一 |
| `test_staging.py` | 暂存→账本：导入门闸、写穿、镜像、删除一致性 |
| `test_shipment.py` | 集运挂靠/解除的原子性、软删联动、金额 |
| `test_tags_dashboard.py` | 标签增删改色/改名/在用保护；看板聚合与排除规则；物品列表 |
| `test_validation.py` | 对抗性输入：NaN/无穷/超大/负数/注入串/类型混淆——**只求绝不 500** |
| `test_lengths.py` | 字符串列长度（元测试：新增列漏加校验就红） |
| `test_edge_cases.py` | 种子价折算、邮费口径、唯一约束在 PATCH 上生效、汇率回退 |
| `test_concurrency.py` | 交错写：`guarded_bump`、导入门闸、挂靠守卫 |
| `test_queries.py` | 查询次数回归：列表接口不得 N+1 |
| `test_softdelete.py` | 软删也要推进 version/updated_at |
| `test_plugins.py` | 插件发现、账号增删改名、**目录穿越防护**、按账号清理 |
| `test_ocr_parse.py` | OCR 纯解析层（不跑引擎、不需要 rapidocr） |
| `test_dbadmin.py` | 迁移表清单完整性、整库拷贝、DSN 加解密、方言助手 |
| `test_consistency.py` | **跨层一致性**：后端枚举 ↔ 前端常量 ↔ 爬虫映射 ↔ 前端调用的 API 路径 |

## 两类值得留意的测试

**`test_consistency.py`** 把「三处各写一份、只能靠约定同步」的常量变成断言：订单状态枚举、
状态生命周期序（`ORDER_STATUS_RANK`）、爬虫的 `STATUS_MAP` 目标值、列布局表白名单、
前端 `api/index.js` 里写死的每条路径。改了后端枚举却忘了改前端/爬虫，这里会红——
本次审计的最高危 bug（爬虫推「交易成功」被后端 422 整批丢弃）就是它抓到的。

**`test_edge_cases.py`** 里有几条注释以「⚠️ 已知行为」开头：那不是 bug，是当前刻意/已知的
取舍（如「清空全部单价会把旧总价折到第一条」）。它们被钉在这里是为了**改动时不会无声漂移**；
若哪天决定改语义，请连带更新这些断言。
