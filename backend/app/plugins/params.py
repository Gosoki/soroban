"""插件私有参数：校验、存取、下发。

与**核心设置**（`services/prefs.SPECS`）的分工，别搞混：
  · 核心设置 —— 跨插件通用的业务偏好（汇率源顺序、过期上限）。定义在核心，
    设置页自动渲染，插件用清单的 `settings = [...]` 声明要读哪几项。
  · 插件参数 —— **只有这个插件懂**的东西（某个 API key、要查哪几家承运商、
    抓多少页）。核心不理解其含义，只负责存、校验类型、渲染控件、下发。

值存在 `PluginConfig.params_json` 的 `opts` 子字典里——那个字段原本只放账号列表，
两者共存是刻意的：一次读写就拿到插件的全部配置，不用再加一列。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .manifest import Manifest, Param

log = logging.getLogger("soroban.plugins.params")

_STR_MAX = 2048          # 单个字符串参数的上限。防一次误粘贴把 params_json 撑爆


def _coerce(p: Param, v: Any) -> Any:
    """把前端/清单来的值收敛成该参数声明的类型。不合规抛 ValueError（路由转 422）。"""
    if v is None or v == "":
        return None if p.type in ("str", "secret", "select") else p.default
    if p.type == "bool":
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)
    if p.type == "int":
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"{p.label} 需要一个整数，收到 {v!r}")
        if p.minimum is not None and n < p.minimum:
            raise ValueError(f"{p.label} 不能小于 {p.minimum}")
        if p.maximum is not None and n > p.maximum:
            raise ValueError(f"{p.label} 不能大于 {p.maximum}")
        return n
    if p.type == "select":
        s = str(v)
        if p.choices and s not in [str(c) for c in p.choices]:
            raise ValueError(f"{p.label} 只能是 {p.choices} 之一，收到 {s!r}")
        return s
    s = str(v)
    if len(s) > _STR_MAX:
        raise ValueError(f"{p.label} 过长（上限 {_STR_MAX} 字符）")
    return s


def load(m: Manifest, cfg) -> dict:
    """本插件参数的当前值（库里没有的用清单默认值补齐）。

    按清单**补齐默认值**而不是只回库里有的：这样插件拿到的永远是完整一份，
    不必自己再写一遍默认值——写两遍必然漂移，而漂移的表现是「界面上显示 A、实际按 B 跑」。
    """
    stored = {}
    if cfg is not None:
        try:
            stored = (json.loads(cfg.params_json or "{}") or {}).get("opts") or {}
        except (TypeError, ValueError):
            log.warning("插件 %s 的参数存储不是合法 JSON，按默认值处理", m.id)
    out = {}
    for p in m.params:
        raw = stored.get(p.key, p.default)
        try:
            out[p.key] = _coerce(p, raw)
        except ValueError as e:                 # 库里存着坏值（手改过/插件降级）
            log.warning("插件 %s 的参数 %s 不可用（%s），退回默认值", m.id, p.key, e)
            out[p.key] = p.default
    return out


def save(m: Manifest, cfg, patch: dict) -> dict:
    """按 patch 更新参数并写回 `cfg.params_json`（不 commit，调用方决定）。

    清单里没声明的键**直接丢弃**：插件降级之后界面上那份旧表单不该让保存整个失败——
    用户会以为是别的地方坏了。丢弃会记一行日志。
    """
    known = {p.key: p for p in m.params}
    unknown = [k for k in patch if k not in known]
    if unknown:
        log.info("插件 %s 收到未声明的参数 %s，已忽略", m.id, unknown)
    cleaned = {k: _coerce(known[k], v) for k, v in patch.items() if k in known}

    try:
        blob = json.loads(cfg.params_json or "{}") or {}
    except (TypeError, ValueError):
        blob = {}
    opts = blob.get("opts") or {}
    opts.update(cleaned)
    blob["opts"] = opts
    cfg.params_json = json.dumps(blob, ensure_ascii=False)
    return load(m, cfg)


def redact(m: Manifest, values: dict) -> dict:
    """写日志前脱敏。secret 参数的值绝不该出现在日志里。"""
    out = dict(values)
    for p in m.params:
        if p.secret and out.get(p.key):
            out[p.key] = "***"
    return out
