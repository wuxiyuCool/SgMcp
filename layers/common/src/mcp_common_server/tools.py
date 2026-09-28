"""【下层】通用工具的实现（独立模块，便于网关本地执行器复用）。

规范（docs/develop-deploy.md「工具清单」）：一个实现函数对应一个 util_* 工具，
每域 ≤8 个；错误一律 "[COMMON_<CODE>] 消息；建议…" 形态，AI 按错误码自行纠正。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import random
import re
import string as stringmod
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import parse_qsl, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.mcpserver.exceptions import ToolError

from mcp_shared.limits import check_pattern, check_text, max_regex_text


def fail(code: str, message: str) -> ToolError:
    """带错误码的工具错误。

    必须用 ToolError 而不是 ValueError：SDK 会把非 ToolError 异常裹成
    UnexpectedToolError 并**丢掉原始消息**，AI 就只剩一句 "Error executing tool"，
    错误码与建议全都白写。
    """
    return ToolError(f"[COMMON_{code}] {message}")


def _zone(name: str):
    """时区解析：把 Python 的 keyless 报错换成 AI 能照着改的引导。"""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise fail(
            "INVALID_INPUT",
            f"无效时区名: {name!r}。请传 IANA 名（Asia/Shanghai、UTC、America/New_York），"
            "不要传「北京」/GMT+8/8 之类",
        ) from e


def _parse_iso(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise fail(
            "INVALID_INPUT",
            f"时间格式无法解析: {value!r}（{e}）。请用 ISO 8601，"
            "如 2026-09-24T09:30:00、2026-09-24 09:30:00 或 2026-09-24",
        ) from e


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

def get_time(timezone_name: str = "Asia/Shanghai", value: str = "", from_tz: str = "UTC",
             offset_days: int = 0, offset_hours: int = 0) -> dict:
    """当前时间 / 时区换算 / 时间偏移（value 留空=取当前时间）。

    所有规模校验集中在这里：AI 生成的入参可能超长，不设限会打满共享工具进程。
    """
    check_text(timezone_name, limit=64, what="timezone_name")
    check_text(from_tz, limit=64, what="from_tz")
    dt = _parse_iso(value) if value else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(from_tz))
    out = dt.astimezone(_zone(timezone_name))
    shifted = out + timedelta(days=int(offset_days), hours=int(offset_hours))
    return {"timezone": timezone_name, "is_now": not value,
            "input": dt.isoformat() if value else None,
            "converted": out.isoformat(), "shifted": shifted.isoformat()}


# ---------------------------------------------------------------------------
# ID / 随机串
# ---------------------------------------------------------------------------

def create_id(kind: Literal["prefixed", "uuid", "random"] = "prefixed", count: int = 1,
              prefix: str = "id", version: Literal[4, 7] = 4, length: int = 16,
              charset: Literal["alnum", "alpha", "hex", "digits"] = "alnum",
              digits_only: bool = False) -> dict:
    """批量生成 ID/随机串，返回 {"ids": [...]}。"""
    count = max(1, min(int(count), 100))
    if kind == "prefixed":
        check_text(prefix, limit=64, what="prefix")
        return {"kind": kind, "ids": [f"{prefix}_{uuid.uuid4().hex[:12]}" for _ in range(count)]}
    if kind == "uuid":
        if version == 4:
            return {"kind": kind, "ids": [str(uuid.uuid4()) for _ in range(count)]}
        if version == 7 and hasattr(uuid, "uuid7"):
            return {"kind": kind, "ids": [str(uuid.uuid7()) for _ in range(count)]}
        raise fail("INVALID_INPUT",
                   f"uuid version={version} 不可用（可选 4/7；uuid7 需 Python 3.14+）")
    if kind == "random":
        length = max(1, min(int(length), 512))
        pools = {"alnum": stringmod.ascii_letters + stringmod.digits,
                 "alpha": stringmod.ascii_letters,
                 "hex": "0123456789abcdef",
                 "digits": stringmod.digits}
        pool = pools["digits"] if digits_only else pools.get(charset)
        if not pool:
            raise fail("INVALID_INPUT", f"charset 可选: {sorted(pools)}")
        return {"kind": kind, "ids": [
            "".join(random.SystemRandom().choice(pool) for _ in range(length))
            for _ in range(count)]}
    raise fail("INVALID_INPUT", f"kind 可选 prefixed/uuid/random，收到 {kind!r}")


# ---------------------------------------------------------------------------
# 编码 / 摘要 / slug
# ---------------------------------------------------------------------------

def get_encoded(text: str, algo: Literal["md5", "sha1", "sha256", "sha512", "base64", "slug"] = "sha256",
                mode: Literal["encode", "decode"] = "encode") -> dict:
    """文本摘要/编解码统一入口。md5~sha512 返回 {"algo","hex"}；base64/slug 返回 {"result"}。"""
    check_text(text)
    if algo in {"md5", "sha1", "sha256", "sha512"}:
        return {"algo": algo, "hex": hashlib.new(algo, text.encode("utf-8")).hexdigest()}
    if algo == "base64":
        if mode == "encode":
            return {"algo": algo, "result": base64.b64encode(text.encode("utf-8")).decode("ascii")}
        try:
            return {"algo": algo,
                    "result": base64.b64decode(text.encode("ascii"), validate=True).decode("utf-8")}
        except (binascii.Error, UnicodeDecodeError, ValueError) as e:
            raise fail("INVALID_INPUT", f"Base64 解码失败: {e}；确认输入是合法 Base64（mode=decode）") from e
    # slug：折叠为小写连字符；中文会被整体去除，中文命名请自行给拼音
    return {"algo": algo,
            "result": re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()}


# ---------------------------------------------------------------------------
# 结构化文本解析
# ---------------------------------------------------------------------------

def get_json(text: str, mode: Literal["validate", "pretty", "minify", "keys"] = "validate") -> dict:
    """JSON 处理：validate/pretty/minify/keys。非法 JSON 正常返回 valid=false+错误位置。"""
    check_text(text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        # 非法 JSON 也正常返回：AI 需要的是「哪里错了」，不是工具异常
        return {"valid": False, "error": str(e), "line": e.lineno, "column": e.colno,
                "hint": "检查该位置的引号/逗号/括号；注释不被 JSON 支持"}
    if mode == "validate":
        return {"valid": True, "type": type(obj).__name__,
                "size": len(text), "top_keys": list(obj)[:20] if isinstance(obj, dict) else None}
    if mode == "pretty":
        return {"valid": True, "result": json.dumps(obj, ensure_ascii=False, indent=2)}
    if mode == "minify":
        return {"valid": True, "result": json.dumps(obj, ensure_ascii=False, separators=(",", ":"))}
    if mode == "keys":
        return {"valid": True, "keys": list(obj) if isinstance(obj, dict) else None}
    raise fail("INVALID_INPUT", "mode 可选 validate/pretty/minify/keys")


def get_url(url: str) -> dict:
    """拆解 URL：scheme/host/port/path/query 参数字典/fragment。"""
    check_text(url, limit=20_000, what="url")
    p = urlparse(url)
    if not p.scheme:
        raise fail("INVALID_INPUT", f"URL 缺少协议头: {url!r}；请传完整 URL（含 https://）")
    return {"scheme": p.scheme, "host": p.hostname, "port": p.port, "path": p.path,
            "query": dict(parse_qsl(p.query)), "fragment": p.fragment or None}


def search_text(pattern: str, text: str, flags_ignorecase: bool = False, limit: int = 50) -> dict:
    """正则搜索：返回匹配列表（含命名分组 groups 与位置 span）。limit 上限 200。"""
    check_pattern(pattern)
    check_text(text, limit=max_regex_text(), what="text")
    rx = re.compile(pattern, re.IGNORECASE if flags_ignorecase else 0)
    limit = max(1, min(int(limit), 200))
    matches = [{"text": m.group(0)[:200], "span": list(m.span()), "groups": m.groupdict() or None}
               for m in rx.finditer(text)][:limit]
    return {"count": len(matches), "matches": matches, "pattern": pattern}


def calc_stats(text: str) -> dict:
    """文本统计：chars 字符数 / chars_no_space 非空白 / words 词数 / lines 行数。"""
    check_text(text)
    return {"chars": len(text),
            "chars_no_space": len(re.sub(r"\s", "", text)),
            "words": len(text.split()),
            "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0)}


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def self_check() -> dict:
    """各 util_* 工具跑一遍并回报规模上限配置（部署完先调它确认服务健康）。"""
    checks: dict[str, Any] = {}
    ok = True
    probes = {
        "util_get_time": lambda: get_time("Asia/Shanghai"),
        "util_create_id": lambda: create_id("prefixed", 2, "chk"),
        "util_get_encoded": lambda: [
            get_encoded("abc", "sha256"),
            get_encoded("abc", "base64"),
            get_encoded(get_encoded("abc", "base64")["result"], "base64", "decode"),
            get_encoded("Deploy API v2!", "slug")],
        "util_get_json": lambda: get_json('{"a":1}', "validate"),
        "util_get_url": lambda: get_url("https://example.com:443/a/b?x=1#f"),
        "util_search_text": lambda: search_text(r"(?P<y>\d{4})-\d{2}", "2026-09", False, 5),
        "util_calc_stats": lambda: calc_stats("a b\nc"),
    }
    for name, fn in probes.items():
        try:
            checks[name] = fn()
        except Exception as e:  # noqa: BLE001 — 自检要把失败如实报出来
            checks[name] = f"FAILED: {type(e).__name__}: {e}"
            ok = False
    from mcp_shared import limits

    return {"ok": ok, "checks": checks,
            "limits": {"max_text_chars": limits.max_text(), "max_list_items": limits.max_list(),
                       "max_regex_text": limits.max_regex_text(),
                       "file_roots": [str(p) for p in limits.file_roots()]}}
