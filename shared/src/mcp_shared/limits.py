"""轻活层（Python 工具）的输入规模与路径围栏。

AI 生成的入参不可信：可能是 200MB 的文本、`.*.*` 型正则、或指向 `/etc/passwd`
与 `C:\\Windows\\...` 的路径。企业部署里这些都会变成 DoS 或数据外读通道，
因此在工具入口统一设限，并给出**可自解释**的错误（告诉调用方上限与正确写法）。

- `MAX_TEXT`：单次处理的字符串长度上限（`MCP_MAX_TEXT_CHARS` 覆盖）
- `MAX_LIST`：单次返回/处理的元素个数上限（`MCP_MAX_LIST_ITEMS` 覆盖）
- `MAX_REGEX_TEXT` / `MAX_PATTERN`：正则匹配规模（ReDoS 主要成本来自文本长度×模式复杂度）
- `safe_path()`：文件类入参的路径围栏，白名单根目录由 `MCP_FILE_ROOTS`
  （Python 层）/ `GO_DATAHUB_FILE_ROOTS`（Go 层）配置，两侧同一语义。
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        val = int(raw)
        return val if val > 0 else default
    except ValueError:
        return default


def max_text() -> int:
    return _int_env("MCP_MAX_TEXT_CHARS", 2_000_000)


def max_list() -> int:
    return _int_env("MCP_MAX_LIST_ITEMS", 1000)


def max_regex_text() -> int:
    return _int_env("MCP_MAX_REGEX_TEXT", 500_000)


def max_pattern() -> int:
    return _int_env("MCP_MAX_PATTERN_CHARS", 500)


def check_text(text: str, *, limit: int | None = None, what: str = "text") -> str:
    """文本规模校验：超限给可读错误（而不是把 worker 打满）。"""
    cap = limit or max_text()
    if not isinstance(text, str):
        raise ValueError(f"{what} 必须是字符串，实为 {type(text).__name__}")
    if len(text) > cap:
        raise ValueError(
            f"{what} 过长（{len(text)} 字符 > 上限 {cap}）。请分段处理，"
            "大文件请用重活层的文件工具（data_file_stats / data_convert_file）"
        )
    return text


def check_list_size(items, *, limit: int | None = None, what: str = "列表") -> None:
    cap = limit or max_list()
    if items is not None and len(items) > cap:
        raise ValueError(f"{what} 超过上限 {cap}（实为 {len(items)} 项），请分批处理")


# ReDoS 经验性护栏：Python re 没有超时机制，"分组内含不定量词 + 分组本身再被
# 量词化" 的形态（(a+)+ / (a|a?)* / (.*)*）会因灾难性回溯把共享进程 CPU 打满。
_NESTED_QUANTIFIER = re.compile(
    r"\((?:[^()]*[*+]|[^()]*\|[^()]*\?|[^()]*\?[^()]*\|)[^()]*\)\s*[*+]|\.\*\.\*"
)


def check_pattern(pattern: str) -> str:
    """正则入口护栏：长度上限 + 明显灾难性回溯形态拦截。

    Python `re` 没有超时，一条 `(a+)+b` 配 30 个 a 就能把进程 CPU 打死——
    共享的工具进程一旦卡住，整条链路的 AI 调用全跟着挂。
    """
    if len(pattern) > max_pattern():
        raise ValueError(f"正则过长（{len(pattern)} > {max_pattern()} 字符）")
    if _NESTED_QUANTIFIER.search(pattern):
        raise ValueError(
            f"正则疑似存在灾难性回溯（嵌套量词/重叠分支）：{pattern!r}。"
            "请改用非回溯写法（原子分组思路：减少 * 与 + 的嵌套），或分多步匹配"
        )
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"正则非法: {e}") from e
    return pattern


def file_roots(env_key: str = "MCP_FILE_ROOTS") -> list[Path]:
    """允许访问的根目录清单；未配置时为空（= 不放开本机文件系统）。"""
    raw = os.environ.get(env_key, "")
    roots = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            roots.append(Path(item).resolve())
    return roots


def safe_path(path: str, *, for_write: bool = False, env_key: str = "MCP_FILE_ROOTS") -> Path:
    """路径围栏：解析软链接后必须落在白名单根目录内，且不得穿越父目录。

    未配置白名单时**拒绝**文件路径类调用（安全默认）——工具应改用显式传内容，
    运维放开时配 `MCP_FILE_ROOTS=/data/exports,/tmp` 即可，无需改代码。
    """
    if not path or not str(path).strip():
        raise ValueError("path 不能为空")
    roots = file_roots(env_key)
    if not roots:
        raise ValueError(
            "本机文件路径未开放：请配 MCP_FILE_ROOTS（逗号分隔的允许根目录）后重试，"
            "或改用直接传内容的工具（如 common 的 hash_text / json_tool）"
        )
    p = Path(str(path).strip()).expanduser()
    try:
        resolved = (p if p.is_absolute() else Path.cwd() / p).resolve()
    except OSError as e:
        raise ValueError(f"路径无法解析: {path!r}（{e}）") from e
    for root in roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if for_write and resolved.exists() and resolved.is_dir():
            raise ValueError(f"目标已是目录，不能写入: {resolved}")
        return resolved
    raise ValueError(
        f"路径越界：{resolved} 不在允许清单内（MCP_FILE_ROOTS={','.join(str(r) for r in roots)}）。"
        "请改用清单内目录，或由运维扩展该配置"
    )
