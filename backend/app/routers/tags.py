"""标签选项：列头可管理的下拉集（如淘宝账号、集运收货人）。字段白名单限定。

- 每个标签持久化一个**颜色序号**（0..N-1）：建标签时分配、之后不再变动，故加/删标签
  不会改动其它标签的颜色（稳定），且前 N 个各不相同（不撞色）。
- 数据里出现过的值（爬虫/直写库写进订单）会**自动登记为标签并分配颜色**，无需手动登记。
- 正在被数据使用中的标签**不可删除**（前端隐藏删除按钮，后端亦拒绝）——避免删掉在用的值。
"""

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlmodel import Session, select

from ..auth import get_current_user
from ..database import get_session
from ..db.dialect import insert_or_ignore, upsert
from ..models import (
    ShipmentOrder,
    StagingItem,
    StagingStatus,
    TagOption,
    Order,
    OrderStaging,
    utcnow,
)
from ..schemas import TagIn, TagOut

router = APIRouter(
    prefix="/api/tags", tags=["tags"], dependencies=[Depends(get_current_user)]
)

_ALLOWED_FIELDS = {"platform_account", "recipient", "platform"}
_N_COLORS = 10   # 与前端 TAG_PALETTE 长度一致

# 每个标签字段 → 数据里承载该值的 (模型, 列)。用于把「数据里出现过的值」并入可选集。
# 淘宝账号同时看正式订单与暂存（爬虫先写暂存，账号即时可选）；收货人看集运订单；来源看正式订单与暂存。
_FIELD_SOURCES = {
    "platform_account": (
        (Order, Order.platform_account),
        (OrderStaging, OrderStaging.platform_account),
    ),
    "recipient": ((ShipmentOrder, ShipmentOrder.recipient),),
    "platform": (
        (Order, Order.platform),
        (OrderStaging, OrderStaging.platform),
    ),
}


def _check_field(field: str) -> None:
    if field not in _ALLOWED_FIELDS:
        raise HTTPException(status_code=422, detail=f"未知标签字段: {field}")


def _data_values(session: Session, field: str) -> set[str]:
    """该字段在数据里实际用到的值（订单/暂存/集运，排除软删）。"""
    out: set[str] = set()
    for model, col in _FIELD_SOURCES.get(field, ()):
        stmt = select(col).where(col.is_not(None)).distinct()
        if hasattr(model, "is_delete"):
            stmt = stmt.where(model.is_delete.is_(False))
        if model is OrderStaging:
            # 已忽略的暂存行是「看过后丢弃」的抓取结果，不算真在用（否则其账号会被误锁、误自动登记）
            stmt = stmt.where(OrderStaging.status != StagingStatus.ignored.value)
        for v in session.exec(stmt).all():
            if v:
                out.add(v)
    return out


def _pick_color(counts: Counter) -> int:
    """挑一个颜色序号：优先没被用过的最小序号（前 N 个不撞色）；都用过了取用得最少的。"""
    for i in range(_N_COLORS):
        if counts[i] == 0:
            return i
    return min(range(_N_COLORS), key=lambda i: (counts[i], i))


def _sync(session: Session, field: str, used: set[str]) -> list[TagOption]:
    """确保该字段所有标签都在库、都有颜色：补登记数据里出现的新值、回填历史空颜色。
    used = 该字段在数据里实际用到的值（由调用方算好传入，见 _list：一次扫描两处用）。"""
    rows = session.exec(
        select(TagOption).where(TagOption.field == field).order_by(TagOption.id)
    ).all()
    counts = Counter(r.color for r in rows if r.color is not None)
    existing = {r.value for r in rows}
    changed = False
    for r in rows:                                  # 回填迁移前遗留的空颜色
        if r.color is None:
            r.color = _pick_color(counts)
            counts[r.color] += 1
            session.add(r)
            changed = True
    for v in sorted(used - existing):               # 自动登记数据里的新值
        color = _pick_color(counts)
        counts[color] += 1
        # 原子去重插入：并发 GET/写同时首见同一新值也安全（撞唯一键则忽略，不会让 GET 抛 409）
        session.execute(insert_or_ignore(
            session.get_bind(), TagOption,
            {"field": field, "value": v, "color": color}, ["field", "value"],
        ))
        changed = True
    if changed:
        session.commit()
        rows = session.exec(
            select(TagOption).where(TagOption.field == field).order_by(TagOption.id)
        ).all()
    return rows


def _list(session: Session, field: str) -> list[TagOut]:
    # _data_values 是几张大表的 DISTINCT 扫描：算一次给 _sync（补登记）和 in_use 共用，
    # 别在同一个请求里扫两遍。
    used = _data_values(session, field)
    rows = _sync(session, field, used)
    return [TagOut(value=r.value, color=r.color, in_use=r.value in used) for r in rows]


@router.get("/{field}", response_model=list[TagOut])
def list_tags(field: str, session: Session = Depends(get_session)):
    _check_field(field)
    return _list(session, field)


@router.post("/{field}", response_model=list[TagOut])
def add_tag(field: str, payload: TagIn, session: Session = Depends(get_session)):
    _check_field(field)
    value = payload.value.strip()
    if not value:
        raise HTTPException(status_code=422, detail="标签不能为空")
    rows = session.exec(select(TagOption).where(TagOption.field == field)).all()
    if not any(r.value == value for r in rows):     # 新值才分配颜色（前 N 个不撞色）
        counts = Counter(r.color for r in rows if r.color is not None)
        color = _pick_color(counts)
        # 原子去重插入：并发/重复添加都安全（撞唯一键则忽略，颜色不生效）
        session.execute(insert_or_ignore(
            session.get_bind(), TagOption,
            {"field": field, "value": value, "color": color}, ["field", "value"],
        ))
        session.commit()
    return _list(session, field)


@router.put("/{field}/color", response_model=list[TagOut])
def set_tag_color(
    field: str,
    value: str = Query(..., max_length=128, description="标签值"),
    color: int = Query(..., ge=0, lt=_N_COLORS, description=f"调色盘序号 0..{_N_COLORS - 1}"),
    session: Session = Depends(get_session),
):
    """手动给某标签改颜色（调色盘 10 色之一）。颜色本是建标签时自动分配、之后不变，这里开手动改的口子。
    用 upsert：标签已在库则改色；只在数据里出现、还没登记的值则顺带登记为该色。"""
    _check_field(field)
    session.execute(upsert(
        session.get_bind(), TagOption,
        {"field": field, "value": value, "color": color},
        ["field", "value"], {"color": color},
    ))
    session.commit()
    return _list(session, field)


# --- 标签值改名：跨表迁移（数据 + 标签），供本路由的 /rename 与 plugins 路由在同一事务里复用 -------

def tag_value_in_use(session: Session, field: str, name: str) -> bool:
    """name 是否已被该标签字段占用：对应数据表（见 _FIELD_SOURCES）里有此值的行，或已登记为标签。改名前防重名用。

    可见性口径必须与 _data_values 一致（排除软删行 + 已忽略暂存），否则一个只存在于软删/已忽略行里的
    「幽灵值」会误判为「在用」，把本可用的新名字挡在改名之外。"""
    for model, col in _FIELD_SOURCES.get(field, ()):
        stmt = select(model.id).where(col == name)
        if hasattr(model, "is_delete"):
            stmt = stmt.where(model.is_delete.is_(False))
        if model is OrderStaging:
            stmt = stmt.where(OrderStaging.status != StagingStatus.ignored.value)
        if session.exec(stmt.limit(1)).first() is not None:
            return True
    return session.exec(
        select(TagOption).where(TagOption.field == field, TagOption.value == name)
    ).first() is not None


def rename_tag_value(session: Session, field: str, old: str, new: str) -> dict:
    """把标签字段 field 的值从 old 改成 new，跨表迁移（**不提交**，由调用方在同一事务里 commit）：
      · 该字段对应的数据表（见 _FIELD_SOURCES）的列值，version/updated_at 自增（守住乐观锁纪律）；
      · 标签 TagOption 直接改值以**保住原颜色**（new 已有标签则合并、弃 old）。
    返回各数据表改动行数（键为模型名）。"""
    now = utcnow()
    counts = {}
    for model, col in _FIELD_SOURCES.get(field, ()):
        vals = {col.key: new}
        if hasattr(model, "version"):
            vals["version"] = model.version + 1
        if hasattr(model, "updated_at"):
            vals["updated_at"] = now
        counts[model.__name__] = session.execute(
            sa_update(model).where(col == old).values(**vals)
        ).rowcount
    old_tag = session.exec(
        select(TagOption).where(TagOption.field == field, TagOption.value == old)
    ).first()
    if old_tag:
        new_tag = session.exec(
            select(TagOption).where(TagOption.field == field, TagOption.value == new)
        ).first()
        if new_tag:                       # new 已有标签 → 合并：留 new 的颜色，删 old
            session.delete(old_tag)
        else:                             # 纯改名：old 标签改值，颜色不变（否则改名后颜色被重排）
            old_tag.value = new
            session.add(old_tag)
    return counts


def delete_account_staging(session: Session, account: str) -> int:
    """硬删某淘宝账号名下的全部暂存行（OrderStaging）连同其物品（StagingItem）。
    先删子表再删父表以满足外键；**不提交**，由调用方在同一事务里 commit。返回删除的暂存行数。"""
    ids = session.exec(
        select(OrderStaging.id).where(OrderStaging.platform_account == account)
    ).all()
    if ids:
        session.execute(sa_delete(StagingItem).where(StagingItem.staging_id.in_(ids)))
        session.execute(sa_delete(OrderStaging).where(OrderStaging.id.in_(ids)))
    return len(ids)


def soft_delete_account_orders(session: Session, account: str) -> int:
    """软删某淘宝账号名下的全部账本订单（Order）：is_delete=True、version/updated_at 自增
    （与单条删除同语义、守乐观锁纪律）。已软删的跳过。**不提交**。返回受影响行数。

    对齐单条 delete_order：软删后把「由这些订单导入而来」的暂存行挂靠清掉、状态回「待处理」，
    避免暂存行永远卡在「已导入」且指向已删订单、无法重新导入（否则即数据「损耗」）。"""
    now = utcnow()
    ids = session.exec(
        select(Order.id).where(
            Order.platform_account == account, Order.is_delete.is_(False)
        )
    ).all()
    if not ids:
        return 0
    session.execute(
        sa_update(Order).where(Order.id.in_(ids))
        .values(is_delete=True, version=Order.version + 1, updated_at=now)
    )
    session.execute(
        sa_update(OrderStaging).where(OrderStaging.imported_order_id.in_(ids))
        .values(imported_order_id=None, status=StagingStatus.pending.value,
                version=OrderStaging.version + 1, updated_at=now)
    )
    return len(ids)


@router.delete("/{field}/{value:path}", response_model=list[TagOut])
def remove_tag(field: str, value: str, session: Session = Depends(get_session)):
    _check_field(field)
    if value in _data_values(session, field):       # 使用中不可删（与前端隐藏删除按钮呼应）
        raise HTTPException(status_code=409, detail="该标签正被数据使用，不能删除")
    row = session.exec(
        select(TagOption).where(TagOption.field == field, TagOption.value == value)
    ).first()
    if row:
        session.delete(row)
        session.commit()
    return _list(session, field)


@router.post("/{field}/rename", response_model=list[TagOut])
def rename_tag(
    field: str,
    old: str = Query(..., description="原标签值"),
    new: str = Query(..., max_length=128, description="新标签值"),
    session: Session = Depends(get_session),
):
    """标签改名：把用到该值的订单迁到新值、并保留标签颜色。
    platform_account 牵连插件磁盘会话/配置，须走插件端点（/api/plugins/taobao/account/rename），此处拒绝。"""
    _check_field(field)
    if field == "platform_account":
        raise HTTPException(status_code=400, detail="淘宝账号改名请走插件端点（含磁盘会话/配置迁移）。")
    new = new.strip()
    old = old.strip()                               # 存库值都是 strip 过的，old 不 strip 会漏匹配→假 404
    if not new:
        raise HTTPException(status_code=422, detail="新名字不能为空")
    if not tag_value_in_use(session, field, old):   # 存在性校验放在 new==old 短路之前：
        raise HTTPException(status_code=404, detail=f"没有这个标签：{old}")   # 不存在的标签即便 old==new 也应 404
    if new == old:
        return _list(session, field)
    if tag_value_in_use(session, field, new):
        raise HTTPException(status_code=409, detail=f"新名字已被占用：{new}")
    rename_tag_value(session, field, old, new)
    session.commit()
    return _list(session, field)
