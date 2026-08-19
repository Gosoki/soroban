"""控制存储：始终在 SQLite（soroban.db）里保存「当前用哪个后端」+「连接过的库」清单。

- 两张控制表 `app_db_config`（当前生效后端 + 加密 DSN）与 `db_connection`（记录连接过的
  MySQL，供一键切换）**永远留在 SQLite**，与可切换的业务数据库解耦。
- 切换是**非破坏性**的：只改连接指向，不清空任何库——故 SQLite 里可能仍留有旧业务数据，
  但权威始终以「当前生效后端」为准（切回 SQLite 前请先「迁移到本地」使其最新）。
- 所有 DSN（含密码）用 SECRET_KEY 派生的 Fernet 密钥**加密**后入库；就算 soroban.db 被拷走
  也拿不到明文密码。SECRET_KEY 变更 → 解密失败 → 视为无配置、回退 SQLite。
- 这两张表**不属于** SQLModel.metadata（独立 MetaData + Core），故 Alembic 永不把它们建到
  MySQL，也不纳入业务迁移链。
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

from ..config import settings

log = logging.getLogger("soroban.db.control")

control_metadata = MetaData()

app_db_config = Table(
    "app_db_config",
    control_metadata,
    Column("id", Integer, primary_key=True),           # 恒为 1（单行配置）
    Column("backend", String(16), nullable=False),     # 'sqlite' | 'mysql'
    Column("mysql_url_enc", Text, nullable=True),      # Fernet 加密后的完整 DSN
    Column("updated_at", DateTime, nullable=False),
)

# 「记录连接过的数据库」——每个用过的 MySQL 目标存一行（本地 SQLite 隐式，不入表），
# 供 UI 一键切换。DSN 同样 Fernet 加密；host/port/username/dbname 明文仅用于展示（无密码）。
db_connection = Table(
    "db_connection",
    control_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("label", String(128), nullable=False),
    Column("backend", String(16), nullable=False),     # 目前仅 'mysql'
    Column("url_enc", Text, nullable=False),            # Fernet 加密的完整 DSN（含密码）
    Column("host", String(255), nullable=True),
    Column("port", Integer, nullable=True),
    Column("username", String(128), nullable=True),    # 展示用；列名避开保留字 user
    Column("dbname", String(128), nullable=True),
    Column("created_at", DateTime, nullable=False),
    Column("last_used_at", DateTime, nullable=True),
)


# 「上一次迁移到某目标时，源库长什么样」——供 switch 判断「迁移之后源库又被改过没有」。
# 放控制库（而非业务库）：它描述的是**迁移过程**，且必须独立于「当前用哪个数据引擎」。
# 按目标标识分行，故先后迁到两个不同目标不会互相覆盖。
migrate_state = Table(
    "migrate_state",
    control_metadata,
    Column("target", String(255), primary_key=True),   # 目标库标识，如 u@h:3306/db 或 sqlite
    Column("fingerprint", Text, nullable=False),        # JSON：各表行数 + 最大 updated_at
    Column("migrated_at", DateTime, nullable=False),
)


def _fernet() -> Fernet:
    # 由 SECRET_KEY 派生 32 字节 → urlsafe base64 → Fernet 密钥
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> Optional[str]:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        log.error("MySQL 连接串解密失败（SECRET_KEY 可能已变更）→ 视为无 MySQL 配置")
        return None


def ensure_schema(engine: Engine) -> None:
    """在控制 SQLite 上建 app_db_config 表（幂等）。"""
    control_metadata.create_all(engine)


def read_config(engine: Engine) -> dict:
    # r""" 是必须的：下面提到了 Windows 路径 `Releases\<VERSION>`，而 `\<` 不是合法转义，
    # 普通字符串里 Python 3.12 起会发 SyntaxWarning（将来是 SyntaxError）。
    r"""返回 {'backend', 'mysql_url', 'degraded'}。无配置时默认 sqlite。

    `degraded` 非空 = **配置里写着 MySQL，但连接串解不开**，已经退回本地 SQLite。
    降级本身是对的（解不开就连不上，不能让应用起不来），但它**必须可见**：
    用户看到的现象是打开应用「账本全空了」，而实际数据好端端在 MySQL 里。

    最容易踩到的路径正是升级：`Releases\<VERSION>` 换了目录、exe 搬过去了、
    `.env` 忘了搬 → SECRET_KEY 变了 → Fernet 解不开旧串 → 静默退回空的本地库。
    原先这条路上只有一行 `log.error`，而分发版的用户根本不会去看日志。
    """
    with engine.connect() as conn:
        row = conn.execute(select(app_db_config).where(app_db_config.c.id == 1)).first()
    if row is None:
        return {"backend": "sqlite", "mysql_url": None, "degraded": ""}
    url = decrypt(row.mysql_url_enc) if row.mysql_url_enc else None
    degraded = ""
    backend = row.backend
    if backend == "mysql" and url is None:
        backend = "sqlite"
        degraded = ("配置里写着 MySQL，但连接串解不开（SECRET_KEY 变过？）——"
                    "已暂时退回本地 SQLite。你的 MySQL 数据没丢，"
                    "恢复原来的 .env 即可；或在本页重新填一次 MySQL 连接。")
    return {"backend": backend, "mysql_url": url, "degraded": degraded}


def write_config(engine: Engine, backend: str, mysql_url: Optional[str] = None) -> None:
    enc = encrypt(mysql_url) if mysql_url else None
    now = dt.datetime.now(dt.timezone.utc)
    with engine.begin() as conn:
        exists = conn.execute(select(app_db_config.c.id).where(app_db_config.c.id == 1)).first()
        if exists:
            conn.execute(
                update(app_db_config).where(app_db_config.c.id == 1)
                .values(backend=backend, mysql_url_enc=enc, updated_at=now)
            )
        else:
            conn.execute(
                insert(app_db_config)
                .values(id=1, backend=backend, mysql_url_enc=enc, updated_at=now)
            )


# --- 记录连接过的数据库（saved connections）-------------------------------------

def list_connections(engine: Engine) -> list[dict]:
    """返回已保存的 MySQL 连接（不含密码，最近使用在前）。

    **不跳过解不开的那些**——原先 docstring 说「跳过」，而当时这里根本不解密，那句话是假的。
    现在会逐条试解一次（只为算出下面那个 `decryptable`，明文不出这个函数）。
    列出来是对的：跳过等于「记录凭空消失」，而用户至少要能看见并删掉它。
    但列出来之后点「切换」会拿到 404「连接不存在或无法解密」——「明明列在这里」
    却说「不存在」，是一条读不懂的死路。所以每条带上 `decryptable`：
    解不开的那些在界面上标出来并禁掉切换/迁移，用户一眼知道该重填密码。
    """
    with engine.connect() as conn:
        rows = conn.execute(
            select(db_connection).order_by(db_connection.c.last_used_at.desc())
        ).all()
    out = []
    for r in rows:
        out.append({
            "id": r.id, "label": r.label, "backend": r.backend,
            "host": r.host, "port": r.port, "user": r.username, "database": r.dbname,
            "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
            # SECRET_KEY 变过之后这条就再也解不开了（Fernet 密钥由它派生）。
            "decryptable": decrypt(r.url_enc) is not None,
        })
    return out


def get_connection_url(engine: Engine, cid: int) -> Optional[str]:
    """取某个已保存连接的明文 DSN（解密）。不存在/解不开返回 None。"""
    with engine.connect() as conn:
        row = conn.execute(select(db_connection).where(db_connection.c.id == cid)).first()
    if row is None:
        return None
    return decrypt(row.url_enc)


def upsert_connection(
    engine: Engine, *, backend: str, url: str,
    host: str, port: int, user: str, database: str, label: Optional[str] = None,
) -> int:
    """记住一个 MySQL 连接（按 host+port+user+db 去重；重复则更新 DSN/最近使用时间）。返回 id。"""
    now = dt.datetime.now(dt.timezone.utc)
    enc = encrypt(url)
    lbl = label or f"{user}@{host}:{int(port)}/{database}"
    with engine.begin() as conn:
        existing = conn.execute(
            select(db_connection.c.id).where(
                db_connection.c.host == host,
                db_connection.c.port == int(port),
                db_connection.c.username == user,
                db_connection.c.dbname == database,
            )
        ).first()
        if existing:
            cid = existing.id
            conn.execute(
                update(db_connection).where(db_connection.c.id == cid)
                .values(url_enc=enc, label=lbl, last_used_at=now)
            )
        else:
            res = conn.execute(
                insert(db_connection).values(
                    label=lbl, backend=backend, url_enc=enc, host=host, port=int(port),
                    username=user, dbname=database, created_at=now, last_used_at=now,
                )
            )
            cid = int(res.inserted_primary_key[0])
    return cid


def touch_connection(engine: Engine, cid: int) -> None:
    """更新「最近使用时间」（切换成功后调用）。"""
    with engine.begin() as conn:
        conn.execute(
            update(db_connection).where(db_connection.c.id == cid)
            .values(last_used_at=dt.datetime.now(dt.timezone.utc))
        )


def delete_connection(engine: Engine, cid: int) -> None:
    with engine.begin() as conn:
        conn.execute(delete(db_connection).where(db_connection.c.id == cid))


# --- 迁移快照指纹 --------------------------------------------------------------

def save_migrate_fingerprint(engine: Engine, target: str, fingerprint: str) -> None:
    """记下「迁移到 target 时，源库的样子」。同一目标重复迁移则覆盖。"""
    now = dt.datetime.now(dt.timezone.utc)
    with engine.begin() as conn:
        exists = conn.execute(
            select(migrate_state.c.target).where(migrate_state.c.target == target)).first()
        if exists:
            conn.execute(update(migrate_state).where(migrate_state.c.target == target)
                         .values(fingerprint=fingerprint, migrated_at=now))
        else:
            conn.execute(insert(migrate_state)
                         .values(target=target, fingerprint=fingerprint, migrated_at=now))


def read_migrate_fingerprint(engine: Engine, target: str) -> Optional[str]:
    """取上次迁移到 target 时记录的源库指纹；没记过返回 None（如服务重启前迁的旧版本）。"""
    with engine.connect() as conn:
        row = conn.execute(
            select(migrate_state.c.fingerprint).where(migrate_state.c.target == target)).first()
    return row[0] if row else None
