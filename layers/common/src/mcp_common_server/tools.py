"""【下层】通用工具的函数实现（独立模块，便于网关本地执行器复用）。"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo


def now(timezone_name: str = "UTC") -> str:
    """返回当前时间。timezone_name 为 IANA 时区名（如 UTC、Asia/Shanghai）。"""
    return datetime.now(ZoneInfo(timezone_name)).isoformat()


def generate_id(prefix: str = "id") -> str:
    """生成全局唯一 ID。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def slugify(text: str) -> str:
    """将文本转为小写连字符 slug。"""
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()


def echo(message: str, uppercase: bool = False) -> str:
    """原样返回消息（连通性测试）。"""
    return message.upper() if uppercase else message


def timestamp() -> int:
    """返回当前 Unix 时间戳（秒）。"""
    return int(time.time())
