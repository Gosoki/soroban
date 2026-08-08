"""运行期设置（存库、页面上可改）。

与 `config.settings` 的分工，别搞混：
  · `config.settings` —— 从 `.env`/环境变量读的**部署级**配置（密钥、监听地址、数据库串）。
    改它要动文件、要重启，也不该让登录用户在网页上改。
  · 本模块 —— **业务偏好**，存 `Setting` 表（key-value），「设置」页上直接改，即时生效。

设计取舍：
  · 每项一个 key、值用 JSON 存。不塞成一个大 blob——那样两个人同时改不同项会互相覆盖，
    加字段也得处理「老 blob 里没有这个键」。
  · **注册表驱动**（`SPECS`）：默认值、类型、取值范围、说明都写在一处。
    校验在这里做一次，路由层不重复；前端的表单元信息也直接从这里生成，
    免得「后端允许 1..10、前端写死 1..5」这种两边各说各话。
  · 读的时候按注册表补齐缺省——库里没有的键直接用默认值，不需要「初始化写入」这一步，
    也就不存在「升级后新键没写进去」的问题。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, NamedTuple, Optional

from sqlmodel import Session, select

from ..models import Setting, utcnow

log = logging.getLogger("soroban.prefs")


class Spec(NamedTuple):
    default: Any
    kind: str                       # "int" | "str" | "list[str]"
    label: str                      # 页面上的名字
    hint: str = ""                  # 页面上的说明
    choices: Optional[list] = None  # str / list[str] 的可选值
    minimum: Optional[int] = None   # int 的下界
    maximum: Optional[int] = None   # int 的上界
    validate: Optional[Callable[[Any], None]] = None
    # 下面三项是**给前端渲染用的元信息**。设置页按它们自动生成表单，页面里不再写死
    # 「渲染哪几个键、什么控件、归到哪张卡片」——加一项设置只改这里，不用回头动前端。
    group: str = "通用"             # 归到哪张卡片
    unit: str = ""                  # 输入框后面的单位（秒/小时/次…）
    requires: Optional[str] = None  # 依赖哪个源被启用；没启用时该项置灰（目前只用于 fx.sources）


# --- 汇率源 ---------------------------------------------------------------------
# 键值与 services/fx.SOURCES 的键一一对应；改这里记得同步那边（tests/test_fx_source.py 钉着）。
FX_SOURCE_CHOICES = ["boc", "google", "erapi"]


def _check_manual_rate(v: Any) -> None:
    """手填汇率：空串 = 没设。非空必须能解析成数、且落在与手填校验同一个区间里。"""
    from decimal import Decimal, InvalidOperation

    from ..config import FX_MAX, FX_MIN

    if not isinstance(v, str):
        raise ValueError("手填汇率要填成文本")
    if not v.strip():
        return
    try:
        d = Decimal(v.strip())
    except InvalidOperation:
        raise ValueError(f"手填汇率 {v!r} 不是一个数")
    if not (FX_MIN <= d <= FX_MAX):
        raise ValueError(f"手填汇率 {d} 不在合理区间 [{FX_MIN}, {FX_MAX}]（1元≈20円）")


def _check_sources(v: Any) -> None:
    if not isinstance(v, list) or not v:
        raise ValueError("汇率源顺序至少要留一个")
    if len(set(v)) != len(v):
        raise ValueError("汇率源顺序里有重复项")
    bad = [x for x in v if x not in FX_SOURCE_CHOICES]
    if bad:
        raise ValueError(f"未知的汇率源：{bad}")


SPECS: dict[str, Spec] = {
    "fx.sources": Spec(
        default=["boc", "google", "erapi"], kind="list[str]", choices=FX_SOURCE_CHOICES,
        label="汇率源优先级",
        hint="从上往下依次尝试。上面的取到了就不会去问下面的。",
        validate=_check_sources, group="汇率",
    ),
    "fx.manual_rate": Spec(
        default="", kind="str",
        label="手填汇率（1元 = ?円）",
        hint="留空 = 不用。**这是系统自带的唯一汇率来源**——自动获取由汇率插件负责，"
             "没装插件（或插件还没跑起来）时，库里一条汇率都没有，新建的订单就没有日元金额。"
             "填上它，首次需要汇率时会按它记一条（来源标为「手填」），账本至少能自洽运转。",
        validate=_check_manual_rate, group="汇率",
    ),
    "fx.stale_hours": Spec(
        default=48, kind="int", minimum=1, maximum=8760,
        label="汇率过期上限（小时）",
        hint="超过这个时长没取到新汇率，就当它已过期：界面上明确标出来，新建订单时也会记一条警告。"
             "它管的是「库里这个值还能不能信」，与下面那个「多久才换源」是两件事，别混用。",
        group="汇率", unit="小时",
    ),
    "fx.attempts": Spec(
        default=3, kind="int", minimum=1, maximum=10,
        label="每个源重试次数",
        hint="同一个源在一轮里最多试几次。次数之间有退避，不是连打。",
        group="汇率", unit="次",
    ),
    "fx.fallback_hours": Spec(
        default=72, kind="int", minimum=0, maximum=720,
        label="降级等待（小时）",
        hint="上面的源连续失败满这么久，才允许用下一个源；在此之前一律沿用上一次取到的值。"
             "填 0 = 一失败就立刻用下一个。",
        group="汇率", unit="小时",
    ),
    "fx.refresh_seconds": Spec(
        default=21600, kind="int", minimum=600, maximum=86400,
        label="刷新间隔（秒）",
        hint="后台多久抓一次。默认 21600 = 6 小时；牌价一天也就更新几次，抓太勤没意义。",
        group="汇率", unit="秒",
    ),
    "fx.boc_column": Spec(
        default="zhzjj", kind="str",
        choices=["hmrj", "cmrj", "mcj", "cmcj", "zhzjj"],
        label="中行取价口径",
        hint="中行一行挂五个价，取哪个。五个价来自同一次请求，换口径不增加访问量。"
             "注：日元的现汇与现钞是同一个价，美元等币种才有差别。",
        group="汇率", requires="boc",
    ),
}


def _coerce(key: str, spec: Spec, raw: Any) -> Any:
    """把外部传进来的值收敛成规范类型，并做范围校验。任何不合规都抛 ValueError。"""
    if spec.kind == "int":
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{spec.label}要填整数")
        if spec.minimum is not None and v < spec.minimum:
            raise ValueError(f"{spec.label}不能小于 {spec.minimum}")
        if spec.maximum is not None and v > spec.maximum:
            raise ValueError(f"{spec.label}不能大于 {spec.maximum}")
    elif spec.kind == "str":
        v = str(raw)
        if spec.choices and v not in spec.choices:
            raise ValueError(f"{spec.label}只能是 {spec.choices} 之一")
    elif spec.kind == "list[str]":
        v = list(raw) if isinstance(raw, (list, tuple)) else raw
    else:                                           # pragma: no cover - 注册表写错才会到这
        raise ValueError(f"未知的设置类型：{spec.kind}")
    if spec.validate:
        spec.validate(v)
    return v


def load(session: Session) -> dict[str, Any]:
    """读全部设置。库里没有的键用默认值补齐——所以加新键不需要任何「初始化写入」。

    库里存着坏值（手改过、或降级后残留的旧格式）时**退回默认值并告警**，不抛异常：
    一个设置项坏掉不该让整个应用起不来，更不该让汇率彻底停摆。
    """
    rows = {r.key: r.value for r in session.exec(select(Setting)).all()}
    out: dict[str, Any] = {}
    for key, spec in SPECS.items():
        raw = rows.get(key)
        if raw is None:
            out[key] = spec.default
            continue
        try:
            out[key] = _coerce(key, spec, json.loads(raw))
        except Exception as e:                      # noqa: BLE001
            log.warning("设置 %s 的值不可用（%s），退回默认值 %r", key, e, spec.default)
            out[key] = spec.default
    return out


def get(session: Session, key: str) -> Any:
    return load(session)[key]


def save(session: Session, patch: dict[str, Any]) -> dict[str, Any]:
    """按 patch 更新（只动传进来的键）。任一项不合规就整体拒绝，不做部分写入——
    半套设置比旧设置更难排查。返回更新后的全量设置。"""
    unknown = [k for k in patch if k not in SPECS]
    if unknown:
        raise ValueError(f"未知的设置项：{unknown}")
    cleaned = {k: _coerce(k, SPECS[k], v) for k, v in patch.items()}   # 先全校验，再落库
    for key, value in cleaned.items():
        row = session.get(Setting, key)
        if row:
            row.value, row.updated_at = json.dumps(value, ensure_ascii=False), utcnow()
        else:
            row = Setting(key=key, value=json.dumps(value, ensure_ascii=False))
        session.add(row)
    session.commit()
    return load(session)


def describe() -> list[dict]:
    """给前端的表单元信息。前端据此渲染，不再自己写一份取值范围——
    「后端允许 1..10、前端写死 1..5」这种两边各说各话，靠这个消掉。"""
    return [
        {"key": k, "kind": s.kind, "label": s.label, "hint": s.hint,
         "choices": s.choices, "min": s.minimum, "max": s.maximum, "default": s.default,
         "group": s.group, "unit": s.unit, "requires": s.requires}
        for k, s in SPECS.items()
    ]
