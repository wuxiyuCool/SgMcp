"""【下层】通用工具的函数实现（独立模块，便于网关本地执行器复用）。

包含两类：平台原有基础件（now/echo/generate_id…）与市面通用 MCP 标准件
（哈希/编解码/JSON/正则/URL/时间换算等轻活档工具）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import random
import re
import string as stringmod
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import parse_qsl, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp_shared.limits import check_pattern, check_text, max_regex_text


def _zone(name: str):
    """时区解析：把 Python 的 keyless 报错换成 AI 能照着改的引导。"""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(
            f"无效时区名: {name!r}。请传 IANA 名（Asia/Shanghai、UTC、America/New_York），"
            "不要传「北京」/GMT+8/8 之类"
        ) from e


def now(timezone_name: str = "UTC") -> str:
    """返回当前时间。timezone_name 为 IANA 时区名（如 UTC、Asia/Shanghai）。"""
    return datetime.now(_zone(timezone_name)).isoformat()


def generate_id(prefix: str = "id") -> str:
    """生成全局唯一 ID。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def slugify(text: str) -> str:
    """将文本转为小写连字符 slug。"""
    check_text(text)
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()


def echo(message: str, uppercase: bool = False) -> str:
    """原样返回消息（连通性测试）。"""
    check_text(message)
    return message.upper() if uppercase else message


def timestamp() -> int:
    """返回当前 Unix 时间戳（秒）。"""
    return int(time.time())


# ---------------------------------------------------------------------------
# 通用文本/编码小工具（轻活档：字符串级内存操作）
#
# 所有接受外部文本的入口都过 mcp_shared.limits 的规模校验：AI 生成的入参可能
# 是几百 MB 字符串或灾难性回溯正则，不设限会把共享工具进程打满。
# ---------------------------------------------------------------------------

def hash_text(text: str, algo: Literal["md5", "sha1", "sha256", "sha512"] = "sha256") -> dict:
    """计算文本哈希。algo: md5/sha1/sha256/sha512。"""
    check_text(text)
    if algo not in {"md5", "sha1", "sha256", "sha512"}:
        raise ValueError(f"不支持的算法: {algo}")
    return {"algo": algo, "hex": hashlib.new(algo, text.encode("utf-8")).hexdigest()}


def base64_codec(text: str, mode: Literal["encode", "decode"] = "encode") -> dict:
    """Base64 编解码。mode=encode（文本→b64）/decode（b64→文本）。"""
    check_text(text)
    if mode == "encode":
        return {"result": base64.b64encode(text.encode("utf-8")).decode("ascii")}
    try:
        return {"result": base64.b64decode(text.encode("ascii"), validate=True).decode("utf-8")}
    except (binascii.Error, UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"Base64 解码失败: {e}") from e


def uuid_generate(count: int = 1, version: Literal[4, 7] = 4) -> dict:
    """批量生成 UUID。count 上限 100；version 支持 4（随机）/7（时间有序，3.14+）。"""
    count = max(1, min(int(count), 100))
    if version == 4:
        ids = [str(uuid.uuid4()) for _ in range(count)]
    elif version == 7 and hasattr(uuid, "uuid7"):
        ids = [str(uuid.uuid7()) for _ in range(count)]
    else:
        raise ValueError(f"不支持的版本: {version}（可选 4/7；uuid7 需 Python 3.14+）")
    return {"ids": ids}


def random_string(length: int = 16, charset: Literal["alnum", "alpha", "hex", "digits"] = "alnum",
                  digits_only: bool = False) -> dict:
    """生成密码学随机字符串。charset: alnum/alpha/hex/digits；digits_only 快捷数字串。"""
    length = max(1, min(int(length), 512))
    pools = {"alnum": stringmod.ascii_letters + stringmod.digits,
             "alpha": stringmod.ascii_letters,
             "hex": "0123456789abcdef",
             "digits": stringmod.digits}
    pool = pools["digits"] if digits_only else pools.get(charset)
    if not pool:
        raise ValueError(f"charset 可选: {sorted(pools)}")
    return {"result": "".join(random.SystemRandom().choice(pool) for _ in range(length))}


def json_tool(text: str, mode: Literal["validate", "pretty", "minify", "keys"] = "validate") -> dict:
    """JSON 处理：validate（合法性+错误位置）/pretty（缩进）/minify（压缩）/keys（顶层键）。"""
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
    raise ValueError("mode 可选 validate/pretty/minify/keys")


def datetime_convert(value: str = "", from_tz: str = "UTC", to_tz: str = "Asia/Shanghai",
                     offset_days: int = 0, offset_hours: int = 0) -> dict:
    """时区转换 + 时间偏移。value 为 ISO 8601 时间串，留空表示当前时间（视为 UTC）。

    返回：converted=换算到 to_tz；shifted=再偏移 offset_days/offset_hours。
    """
    dt = _parse_iso(value) if value else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(from_tz))
    out = dt.astimezone(_zone(to_tz))
    shifted = out + timedelta(days=int(offset_days), hours=int(offset_hours))
    return {"input": dt.isoformat(), "converted": out.isoformat(),
            "shifted": shifted.isoformat(), "to_tz": to_tz}


def _parse_iso(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(
            f"时间格式无法解析: {value!r}（{e}）。请用 ISO 8601，"
            "如 2026-09-24T09:30:00、2026-09-24 09:30:00 或 2026-09-24"
        ) from e


def url_parse(url: str) -> dict:
    """拆解 URL：scheme/host/port/path/query 参数字典/fragment。"""
    check_text(url, limit=20_000, what="url")
    p = urlparse(url)
    return {"scheme": p.scheme, "host": p.hostname, "port": p.port, "path": p.path,
            "query": dict(parse_qsl(p.query)), "fragment": p.fragment or None}


def regex_find(pattern: str, text: str, flags_ignorecase: bool = False, limit: int = 50) -> dict:
    """正则搜索：返回匹配列表（含命名分组 groups 与位置 span）。limit 上限 200。"""
    check_pattern(pattern)
    check_text(text, limit=max_regex_text(), what="text")
    rx = re.compile(pattern, re.IGNORECASE if flags_ignorecase else 0)
    limit = max(1, min(int(limit), 200))
    matches = [{"text": m.group(0)[:200], "span": list(m.span()), "groups": m.groupdict() or None}
               for m in rx.finditer(text)][:limit]
    return {"count": len(matches), "matches": matches, "pattern": pattern}


def text_stats(text: str) -> dict:
    """文本统计：chars 字符数 / chars_no_space 非空白 / words 词数 / lines 行数。"""
    check_text(text)
    return {"chars": len(text),
            "chars_no_space": len(re.sub(r"\s", "", text)),
            "words": len(text.split()),
            "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0)}
