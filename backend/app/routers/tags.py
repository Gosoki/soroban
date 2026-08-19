"""标签选项：列头可管理的下拉集（如淘宝账号、集运收货人）。字段白名单限定。

- 每个标签持久化一个**颜色序号**（0..N-1）：建标签时分配、之后不再变动，故加/删标签
  不会改动其它标签的颜色（稳定），且前 N 个各不相同（不撞色）。
- 数据里出现过的值（爬虫/直写库写进订单）会**自动登记为标签并分配颜色**，无需手动登记。
- 正在被数据使用中的标签**不可删除**（前端隐藏删除按钮，后端亦拒绝）——避免删掉在用的值。
"""

import logging
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlmodel import Session, select

from ..auth import get_current_user
from ..database import get_session
from ..db.dialect import insert_or_ignore, upsert
from ..maintenance import barrier
from ..models import (
    ShipmentOrder,
    StagingItem,
    ImportStatus,
    TagOption,
    Order,
    OrderStaging,
    utcnow,
)
from ..schemas import TagIn, TagOut

router = APIRouter(
    prefix="/api/tags", tags=["tags"], dependencies=[Depends(get_current_user)]
)

log = logging.getLogger("soroban")

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
        raise HTTPException(status_code=422, detail=f"未知标签字段：{field}")


def _data_values(session: Session, field: str) -> set[str]:
    """该字段在数据里实际用到的值（订单/暂存/集运，排除软删）。"""
    out: set[str] = set()
    for model, col in _FIELD_SOURCES.get(field, ()):
        stmt = select(col).where(col.is_not(None)).distinct()
        if hasattr(model, "is_delete"):
            stmt = stmt.where(model.is_delete.is_(False))
        if model is OrderStaging:
            # 已忽略的暂存行是「看过后丢弃」的抓取结果，不算真在用（否则其账号会被误锁、误自动登记）
            stmt = stmt.where(OrderStaging.import_status != ImportStatus.ignored.value)
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
    used = 该字段在数据里实际用到的值（由调用方算好传入，见 _list：一次扫描两处用）。

    **这是唯一一条经 GET 请求写库的路径**（GET /api/tags/{field} → _list → 这里 commit）。
    HTTP 中间件按「GET 是安全方法」直接放行，拦不住它——所以只读屏障必须在这里自己查一次，
    否则数据库迁移拷贝期间随便打开一个带标签列的列表页，就会往源库写 tagoption，
    正是 maintenance.py 要杜绝的撕裂拷贝。
    屏障期间只是**不落库**：已登记的标签照常返回，未登记的新值这次不登记，
    下一次 GET 自然补上——纯粹是推迟，不丢任何东西。"""
    rows = session.exec(
        select(TagOption).where(TagOption.field == field).order_by(TagOption.id)
    ).all()
    if barrier.blocked_reason() is not None:
        return rows
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
    # 在**登记时**就按目标数据列限长：TagOption.value 能存 128，但选进 Order.platform(32) 会炸。
    # 不在这里挡，用户会建出一个「选一次报一次错」的标签。
    check_value_fits(field, value)
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
    # **和 add_tag / rename_tag 同一道口径。** 这里用的是 upsert：没登记过的值会被
    # **顺带创建**，而这个端点原先既不 strip 也不判空 ⇒ `?value=%20%20` / `?value=`
    # 都会 200 并在库里留下一个 `'  '` / `''` 的标签。它们永远不可能 in_use
    # （所有写入口都 strip），于是常驻下拉框。对照：`POST /api/tags/{field} {"value":"  "}` 是 422。
    value = value.strip()
    if not value:
        raise HTTPException(status_code=422, detail="标签值不能为空")
    check_value_fits(field, value)      # 它会顺带登记该值，同样得先确认装得进数据列
    session.execute(upsert(
        session.get_bind(), TagOption,
        {"field": field, "value": value, "color": color},
        ["field", "value"], {"color": color},
    ))
    session.commit()
    return _list(session, field)


# --- 标签值改名：跨表迁移（数据 + 标签），供本路由的 /rename 与 plugins 路由在同一事务里复用 -------

def _column_of(model, field: str):
    """取 `model` 上名为 `field` 的那一列——**只认 `_FIELD_SOURCES` 里登记过的字段**。

    不查白名单直接 `getattr(model, field)` 的话，插件清单里写什么就查什么列，
    等于把「查/删哪一列」的决定权交给了一份手写 toml。
    """
    for m, col in _FIELD_SOURCES.get(field, ()):
        if m is model:
            return col
    raise HTTPException(status_code=400, detail=f"不支持的账号字段：{field}")


def tag_value_in_use(session: Session, field: str, name: str) -> bool:
    """name 是否已被该标签字段占用：对应数据表（见 _FIELD_SOURCES）里有此值的行，或已登记为标签。改名前防重名用。

    可见性口径必须与 _data_values 一致（排除软删行 + 已忽略暂存），否则一个只存在于软删/已忽略行里的
    「幽灵值」会误判为「在用」，把本可用的新名字挡在改名之外。"""
    for model, col in _FIELD_SOURCES.get(field, ()):
        stmt = select(model.id).where(col == name)
        if hasattr(model, "is_delete"):
            stmt = stmt.where(model.is_delete.is_(False))
        if model is OrderStaging:
            stmt = stmt.where(OrderStaging.import_status != ImportStatus.ignored.value)
        if session.exec(stmt.limit(1)).first() is not None:
            return True
    return session.exec(
        select(TagOption).where(TagOption.field == field, TagOption.value == name)
    ).first() is not None


def check_value_fits(field: str, value: str) -> None:
    """标签值必须能装进它会被写入的**每一个**数据列，否则 422。

    请求体 schema 那套 max_length（见 schemas._len）只覆盖 body 字段；标签的增加/改名走的是
    **Query 参数**，绕开了那层。而 TagOption.value 是 VARCHAR(128)、目标数据列却可能只有
    VARCHAR(32)（如 Order.platform）——超长时 MySQL 抛 DataError（不是 IntegrityError，
    逃过全局 409 处理器 → 500），SQLite 则静默存下，之后迁到 MySQL 时整次 replace_data 被这条
    脏数据卡死。所以按**最小的那个列**限长。"""
    for model, col in _FIELD_SOURCES.get(field, ()):
        limit = model.__table__.columns[col.key].type.length
        if limit and len(value) > limit:
            raise HTTPException(
                status_code=422,
                detail=f"「{field}」的值最长 {limit} 个字符（当前 {len(value)}）",
            )


def rename_tag_value(session: Session, field: str, old: str, new: str) -> dict:
    """把标签字段 field 的值从 old 改成 new，跨表迁移（**不提交**，由调用方在同一事务里 commit）：
      · 该字段对应的数据表（见 _FIELD_SOURCES）的列值，version/updated_at 自增（守住乐观锁纪律）；
      · 标签 TagOption 直接改值以**保住原颜色**（new 已有标签则合并、弃 old）。
    返回各数据表改动行数（键为模型名）。"""
    check_value_fits(field, new)
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


def delete_account_staging(session: Session, field: str, account: str) -> tuple[int, int]:
    """硬删某账号名下的暂存行（OrderStaging）连同其物品（StagingItem）。返回 (删除数, 跳过数)。

    `field` = 账号名落在暂存表的哪一列，由插件清单的 `accounts_ledger_field` 决定。
    原先这里写死 `platform_account`，而上游只拿 `ledger_field` 当「有没有声明」的开关用——
    以后哪个插件声明成别的列，会顺利通过校验，然后删掉**另一列**同名的行。
    半截抽象比没抽象更危险：它看起来是通用的。
    先删子表再删父表以满足外键；**不提交**，由调用方在同一事务里 commit。

    **跳过「已导入且账本单仍在」的行**——与单条删除（routers/staging.delete_staging 的 409）
    同一条不变量：暂存行是账本单的镜像，把镜像删了会留下一张没人认领的账本单，它继续占着
    (order_no, platform) 唯一号；爬虫下次重抓时暂存唯一索引已空出、会新建一条待处理行，
    用户点「导入」就撞唯一约束、再也导不进来。要真删得先在「商品订单」页删掉账本单。"""
    rows = session.exec(
        select(OrderStaging.id, OrderStaging.imported_order_id)
        .where(_column_of(OrderStaging, field) == account)
    ).all()
    linked_ids = [r[1] for r in rows if r[1] is not None]
    alive = set()
    if linked_ids:
        alive = set(session.exec(
            select(Order.id).where(Order.id.in_(linked_ids), Order.is_delete.is_(False))
        ).all())
    ids = [r[0] for r in rows if r[1] not in alive]      # None not in alive → 未导入的照删
    if ids:
        session.execute(sa_delete(StagingItem).where(StagingItem.staging_id.in_(ids)))
        session.execute(sa_delete(OrderStaging).where(OrderStaging.id.in_(ids)))
    return len(ids), len(rows) - len(ids)


def soft_delete_account_orders(session: Session, field: str, account: str) -> int:
    """软删某账号名下的全部账本订单（Order）：is_delete=True、version/updated_at 自增
    （与单条删除同语义、守乐观锁纪律）。已软删的跳过。**不提交**。返回受影响行数。

    对齐单条 delete_order：软删后把「由这些订单导入而来」的暂存行挂靠清掉、状态回「待处理」，
    避免暂存行永远卡在「已导入」且指向已删订单、无法重新导入（否则即数据「损耗」）。"""
    now = utcnow()
    ids = session.exec(
        select(Order.id).where(
            _column_of(Order, field) == account, Order.is_delete.is_(False)
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
        .values(imported_order_id=None, import_status=ImportStatus.pending.value,
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


def _plugin_owns_account(session: Session, field: str, value: str) -> bool:
    """`value` 是不是**某个插件正在管的账号名**（而不只是「这一列归插件管」）。

    判据从字段级收紧到值级，因为字段级会把手工录单的账号一起锁死：
    只要装了任何一个声明 `accounts_ledger_field = "platform_account"` 的插件，
    整列的所有值都被这里 400 打回、叫人「去插件管理页操作」——而插件卡片
    **只列它自己的账号**（配置里的 ∪ state 目录里的），手工录入的名字在那页上
    根本不存在，删又删不掉（DELETE 报 409 in_use）。于是那个名字改不了也删不掉，
    界面上还没有任何地方解释为什么。

    值级判据把两种情形分开了：
      · 插件管的账号 → 仍然拒绝（改名要连磁盘会话与插件配置一起迁，只有插件端点会做）；
      · 手工录的账号 → 正常改名，本来就没有别处的状态需要跟着动。

    探测失败一律当「不归插件管」——最坏是本地改个名（数据自身仍然一致），
    而反过来误判会把用户堵死在一个他打不开的端点上。
    惰性 import：`plugins.py` 已经 import 了本模块，顶层互相 import 会成环。
    """
    try:
        from .plugins import PluginConfig, _known_names, _ledger_field, discover

        for m in discover():
            if _ledger_field(m) != field:               # 这个插件的账号不落在这一列上
                continue
            cfg = session.get(PluginConfig, m["id"])
            if value in _known_names(cfg, m):           # 配置账号 ∪ 磁盘残留会话
                return True
        return False
    except Exception as e:                                   # noqa: BLE001
        log.warning("判断 %s=%s 是否由插件管理时出错，按「无插件」处理：%s", field, value, e)
        return False


@router.post("/{field}/rename", response_model=list[TagOut])
def rename_tag(
    field: str,
    old: str = Query(..., description="原标签值"),
    new: str = Query(..., max_length=128, description="新标签值"),
    session: Session = Depends(get_session),
):
    """标签改名：把用到该值的订单迁到新值、并保留标签颜色。

    **有插件声明这一列时才拒绝，而不是按列名写死。**
    原先无条件对 `platform_account` 报 400、让人「走插件端点」，而前端那条路又把
    `taobao` 焊在 URL 里。于是插件目录不在时（源码安装、或自定 PLUGIN_DIR），
    手工录单产生的账号名**既删不掉也改不了名**：DELETE 返回 409（in_use）、
    这里 400 让你走插件、插件端点 404「未发现插件：taobao」——
    而列头那颗改名笔对所有 tag 列无条件渲染，用户只会看到一句和操作毫不相干的
    「未发现插件：taobao」。

    判据必须是「**有没有插件声明这个字段**」（清单的 `accounts_ledger_field`），
    不能是「插件缺失就跳过迁移」——后者会在插件只是暂时没装好时，
    把账本改了而磁盘会话与插件配置留在旧名下，下一轮抓取又把旧名建回来。
    """
    _check_field(field)
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
    # 两端都查，理由不同：改**插件账号**要连磁盘会话与插件配置一起迁（只有插件端点会做）；
    # 改成插件账号名则会让手工数据和插件抓来的数据在账本里合流，插件下次抓取还会把它当自己的。
    if _plugin_owns_account(session, field, old):
        raise HTTPException(
            status_code=400,
            detail=f"「{old}」是插件管理的账号，改名请在「插件管理」页操作（要一并迁移磁盘登录会话与插件配置）")
    if _plugin_owns_account(session, field, new):
        raise HTTPException(
            status_code=409,
            detail=f"「{new}」是插件管理的账号，不能改成它（会和插件抓来的数据混在一起）")
    rename_tag_value(session, field, old, new)
    session.commit()
    return _list(session, field)
