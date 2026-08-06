#!/usr/bin/env bash
# soroban 账本备份（WAL 安全）。用 sqlite3 .backup 而非裸 copy，避免漏掉 -wal 未 checkpoint 的数据。
# 建议加进 cron，例如每天 03:00：  0 3 * * * /path/to/soroban/backup.sh >> /path/to/soroban/backups/backup.log 2>&1
#
# ⚠️ 本脚本只备份**本地 SQLite**。业务数据可能已经在应用内「数据库」页被切到 MySQL——
#    而切换是非破坏性的（不清空 SQLite），soroban.db 里会留着切换那一刻的完整旧表。
#    所以在 MySQL 模式下照跑，会产出一份「体积正常、表齐全、能打开」但**停在迁移当天**的备份，
#    退出码还是 0、cron 日志天天成功。真出事去恢复才发现丢了几个月的账。
#    → 下面显式检测当前后端，MySQL 模式一律**报错退出**，绝不静默返回成功。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB="$ROOT/backend/soroban.db"
OUT="$ROOT/backups"

[ -f "$DB" ] || { echo "找不到数据库 $DB"; exit 1; }
command -v sqlite3 >/dev/null || { echo "缺少 sqlite3 命令，无法备份"; exit 1; }

# 当前生效后端存在控制表 app_db_config 里（恒在 SQLite，见 backend/app/db/control.py）。
# 表不存在（老库/全新库）时 sqlite3 会报错，用 || true 兜住并当作 sqlite 处理。
BACKEND="$(sqlite3 "$DB" "SELECT backend FROM app_db_config WHERE id=1;" 2>/dev/null || true)"
if [ "$BACKEND" = "mysql" ]; then
  cat >&2 <<'EOF'
[X] 当前业务数据在 MySQL 上，本脚本备份的 SQLite 只是「切换前的旧快照」，备了也没用。
    请改用 mysqldump 备份真正的库，例如：
      mysqldump --single-transaction --default-character-set=utf8mb4 <库名> > soroban-$(date +%F).sql
    连接参数见应用内「数据库」页（密码在库里是加密存的，脚本取不到）。
    若你确实只想留一份 SQLite 控制库快照，请显式设 SOROBAN_BACKUP_SQLITE_ANYWAY=1 再跑。
EOF
  [ "${SOROBAN_BACKUP_SQLITE_ANYWAY:-}" = "1" ] || exit 1
  echo "[!] SOROBAN_BACKUP_SQLITE_ANYWAY=1：仍然备份 SQLite 控制库（不含 MySQL 上的业务数据）" >&2
fi

mkdir -p "$OUT"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$OUT/soroban-$STAMP.db"

sqlite3 "$DB" ".backup '$DEST'"
echo "已备份 → $DEST"

# 只保留最近 30 份
ls -1t "$OUT"/soroban-*.db 2>/dev/null | tail -n +31 | xargs -r rm -f
