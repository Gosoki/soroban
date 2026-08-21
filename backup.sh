#!/usr/bin/env bash
# soroban 账本备份。实现在 backend/app/backup.py，这里只是个薄壳。
#
# 建议加进 cron，例如每天 03:00：
#   0 3 * * * /path/to/soroban/backup.sh >> /path/to/soroban/backend/backups/backup.log 2>&1
#
# 这个脚本上一版是直接调 sqlite3 / 让人改用 mysqldump 的，两条路都不成立：
#   · 部署机上 `sqlite3` 和 `mysqldump` **可能一个都没装**（本项目的开发机就是），
#     于是 `command -v sqlite3 || exit 1` 直接退出——「有一个备份脚本」和「有备份」是两回事；
#   · 打包成 exe 之后更没有这两个命令；
#   · MySQL 模式下它只能报错让人手工 mysqldump，而密码在库里是加密存的，脚本取不到。
# 现在改成走应用自己的迁移引擎（`replace_data`），两种后端对称、纯 Python、
# **备的一定是当前生效的那个库**，不会出现「备了一份停在切换当天的 SQLite」。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/backend/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

cd "$ROOT/backend"
exec "$PY" -m tools.backup_db "$@"
