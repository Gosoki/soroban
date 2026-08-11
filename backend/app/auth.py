"""Auth: password hashing, JWT issue/verify, current-user dependency."""

import datetime as dt
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlmodel import Session, select

from .config import settings
from .database import get_session
from .models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user: User, expires_delta: Optional[dt.timedelta] = None) -> str:
    """签发登录令牌。

    **设计取舍（已拍板，勿再当 bug「修」）**：改密码**不会**让已签发的令牌失效。令牌里没有
    版本号、服务端也没有黑名单——这是刻意的：本系统是自用账本，共用者就两个人，为「把某人
    踢下线」这个几乎用不到的能力去加一列 + 迁移 + 每次请求多查一次，不划算。
    真需要强制全体下线时：改 .env 的 SECRET_KEY 并重启即可（代价是所有人重新登录，
    且已保存的 MySQL 连接串会解不开、退回本地 SQLite——见 db/control.py）。"""
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "iat": now,
        "exp": now + (expires_delta or dt.timedelta(days=settings.TOKEN_EXPIRE_DAYS)),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


# 用户名不存在时拿来「陪跑」一次 bcrypt 的哈希。值本身无所谓（永远不会有人猜中它对应的口令），
# 要的只是让两条分支花掉同样的时间。模块加载时算一次，不进每次请求的开销。
_DUMMY_HASH = hash_password("soroban-timing-equalizer")


def authenticate(session: Session, username: str, password: str) -> Optional[User]:
    """校验用户名口令。**用户名存在与否要花同样的时间。**

    原先不存在的用户名直接短路返回、根本不跑 bcrypt，而 bcrypt 是几十毫秒量级——
    响应时间差一个数量级，等于把「哪些用户名是真的」100% 可判别地告诉外面。
    对本项目还有第二重代价：`ratelimit._prune` 的注释里写着，猜不存在的用户名
    「几乎零成本」正是挤兑退避表的手段。让它也付一次 bcrypt 的钱，那条路一起变贵。
    """
    user = session.exec(select(User).where(User.username == username)).first()
    if not user or not user.is_active:
        verify_password(password, _DUMMY_HASH)      # 陪跑：与命中分支等时
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    session: Session = Depends(get_session),
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效或过期的登录凭证",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id = int(payload.get("sub"))
    except (JWTError, TypeError, ValueError):
        raise credentials_error
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise credentials_error
    if payload.get("typ") == "plugin":
        # 插件令牌必须**先过 scope 闸门**才算数。这一句是兜底：万一中间件顺序被人调错、
        # 或有人在非 HTTP 路径上复用了这个依赖，插件令牌不会因此变成全权令牌。
        if not getattr(request.state, "plugin_scope_ok", False):
            raise credentials_error
        # 路由需要知道「是哪个插件、拿了哪些权限」——挂在 user 上而不是再解一次令牌。
        user._plugin_claims = payload
    # `session.get(User, ...)` 开了一个读事务，而 `get_session` 的生成器要兜到**响应结束**
    # 才关。对普通路由无所谓（毫秒级），但 OCR 路由的推理要跑好几秒到十几秒——
    # 那期间这条连接一直是「idle in transaction」。在 MySQL 上它会一路攥着
    # REPEATABLE READ 快照与 MDL，让并发的 DDL/迁移一起等；同时并发上传几张图，
    # 池（20+20）也更容易见底。
    #
    # 这一行**同时还把连接还回了池里**——早先这里写的是「只结束事务、不还连接，还连接
    # 要改 get_session 的生命周期」，那句话是错的：SQLAlchemy 的 Session 在 rollback 时
    # 就把连接 release 回池（实测 `pool.checkedout()` 1→0），下次查询自动开新事务、
    # 重新取一条。所以审计报告 T14 说的「根治」并不需要动 get_session 的生命周期，
    # 这一行就是根治——代价接近零：user 已经读出来了，后面路由自己的查询会开新事务。
    # 唯一还需要单独处理的是「在推理**之前**自己查过库」的路由（见 shipment.ocr_attach_express）。
    # 用 rollback 而不是 commit：这里一个字节都没写，commit 语义上是错的，
    # 而且会把**路由还没提交的写**一起提交掉——依赖是在路由函数之前跑的，
    # 今天没有写，但 commit 会让「以后有人在依赖里写一笔」变成一个静默的越界提交。
    session.rollback()
    return user
