"""【下层】通用工具 MCP server 入口。

提供与业务无关的通用工具（实现见 tools.py），是三层中最底层的基础服务。
入参枚举一律用 `Literal`——生成 JSON Schema enum 让 AI 第一次就填对，
比运行时报错省一轮往返（与 it_ops 的 dataset 枚举同一做法）。

运行：
- stdio：  common-server
- HTTP：   common-server --transport http --port 9100
"""

from __future__ import annotations

from typing import Literal

from mcp.server import MCPServer

from mcp_common_server import tools
from mcp_shared import run_server

mcp = MCPServer("common-tools")


@mcp.tool()
def now(timezone_name: Literal["UTC", "Asia/Shanghai", "Asia/Tokyo", "Europe/London",
                               "America/New_York", "Asia/Hong_Kong"] = "UTC") -> str:
    """返回当前时间（ISO 8601 字符串，如 "2026-09-24T09:30:00+08:00"）。

    参数 timezone_name：IANA 时区名枚举（默认 UTC）。需要其他时区请传最接近的
    常用值，或改用 datetime_convert（它接受任意 IANA 名）。
    """
    return tools.now(timezone_name)


@mcp.tool()
def generate_id(prefix: str = "id") -> str:
    """生成全局唯一 ID，返回字符串，形如 "ticket_a1b2c3d4e5f6"。

    参数 prefix：ID 前缀（小写英文为佳），如 "order" → "order_a1b2c3d4e5f6"。
    每次调用结果必然不同，可用于幂等键/追踪号——经网关 gateway_call 的
    idempotency_key 传回即可让写操作重试安全（同键不会二次执行）。
    """
    return tools.generate_id(prefix)


@mcp.tool()
def slugify(text: str) -> str:
    """把任意文本转为小写连字符 slug（用于命名/文件名/URL）。

    例："Deploy API v2!" → "deploy-api-v2"。仅保留字母数字，其余字符折叠为单个 "-"。
    注意：中文会被整体去除（结果可能为空串），中文命名请自行给拼音。
    """
    return tools.slugify(text)


@mcp.tool()
def echo(message: str, uppercase: bool = False) -> str:
    """原样返回消息。用于连通性/冒烟测试。

    参数：message=要回显的文本（必填）；uppercase=true 时返回其大写形式。
    例：{"message": "abc", "uppercase": true} → "ABC"。
    """
    return tools.echo(message, uppercase)


@mcp.tool()
def timestamp() -> int:
    """返回当前 Unix 时间戳（秒，整数）。无参数。"""
    return tools.timestamp()


@mcp.tool()
def server_self_check() -> dict:
    """下层自检：各工具跑一遍并回报规模上限配置（部署完先调它确认服务健康）。

    返回 {"ok": true, "checks": {...}, "limits": {...}}；任一项失败 ok=false 且
    details 里给出该项错误。运维巡检与 CI 起步用，替代手工逐个 ping。
    """
    checks: dict[str, object] = {}
    ok = True
    probes = {
        "now": lambda: tools.now("Asia/Shanghai"),
        "echo": lambda: tools.echo("ping", True),
        "hash_text": lambda: tools.hash_text("abc", "sha256"),
        "base64_codec": lambda: tools.base64_codec(tools.base64_codec("abc")["result"], "decode"),
        "uuid_generate": lambda: tools.uuid_generate(2, 4),
        "random_string": lambda: tools.random_string(12, "alnum", False),
        "json_tool": lambda: tools.json_tool('{"a":1}', "validate"),
        "datetime_convert": lambda: tools.datetime_convert("2026-01-01T00:00:00", "UTC", "Asia/Shanghai", 0, 0),
        "url_parse": lambda: tools.url_parse("https://example.com:443/a/b?x=1#f"),
        "regex_find": lambda: tools.regex_find(r"(?P<y>\d{4})-\d{2}", "2026-09", False, 5),
        "text_stats": lambda: tools.text_stats("a b\nc"),
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


# ---------------- 通用文本/编码小工具（轻活档） ----------------

@mcp.tool()
def hash_text(text: str, algo: Literal["md5", "sha1", "sha256", "sha512"] = "sha256") -> dict:
    """计算文本哈希值，返回 {"algo", "hex"}。

    参数：text=待哈希文本（必填，长度受限）；algo=枚举 md5/sha1/sha256/sha512（默认 sha256）。"""
    return tools.hash_text(text, algo)


@mcp.tool()
def base64_codec(text: str, mode: Literal["encode", "decode"] = "encode") -> dict:
    """Base64 编码/解码，返回 {"result"}。参数：text=输入；mode=encode（文本→Base64）/decode（Base64→文本）。"""
    return tools.base64_codec(text, mode)


@mcp.tool()
def uuid_generate(count: int = 1, version: Literal[4, 7] = 4) -> dict:
    """批量生成 UUID，返回 {"ids": [...]}。参数：count=数量（默认 1，上限 100）；version=4 随机 / 7 时间有序。"""
    return tools.uuid_generate(count, version)


@mcp.tool()
def random_string(length: int = 16, charset: Literal["alnum", "alpha", "hex", "digits"] = "alnum",
                  digits_only: bool = False) -> dict:
    """生成密码学安全随机串（临时密码/验证码/唯一后缀）。

    参数：length=长度（默认 16，上限 512）；charset=枚举 alnum/alpha/hex/digits；
    digits_only=true 快捷生成纯数字串。"""
    return tools.random_string(length, charset, digits_only)


@mcp.tool()
def json_tool(text: str, mode: Literal["validate", "pretty", "minify", "keys"] = "validate") -> dict:
    """JSON 工具箱。参数：text=JSON 文本；mode=枚举 validate（校验+错误位置）/pretty（美化）/minify（压缩）/keys（顶层键）。

    validate 对非法 JSON 也正常返回 {"valid": false, "error", "line", "column", "hint"}，
    不报工具错误——让 AI 直接看到错在哪一行而不是重试。"""
    return tools.json_tool(text, mode)


@mcp.tool()
def datetime_convert(value: str = "", from_tz: str = "UTC", to_tz: str = "Asia/Shanghai",
                     offset_days: int = 0, offset_hours: int = 0) -> dict:
    """时区换算 + 时间偏移计算。

    参数：value=ISO 8601 时间串（留空=当前时间；也接受 "2026-09-24" 与 "Z" 后缀）；
    from_tz=无时区标记的输入按此时区解释（任意 IANA 名）；
    to_tz=目标时区（IANA 名，如 Asia/Shanghai）；offset_days/offset_hours=偏移（可为负）。
    返回 input/converted/shifted 三个时间点。"""
    return tools.datetime_convert(value, from_tz, to_tz, offset_days, offset_hours)


@mcp.tool()
def url_parse(url: str) -> dict:
    """拆解 URL，返回 scheme/host/port/path/query（参数字典）/fragment。"""
    return tools.url_parse(url)


@mcp.tool()
def regex_find(pattern: str, text: str, flags_ignorecase: bool = False, limit: int = 50) -> dict:
    """正则搜索，返回 {"count", "matches": [{"text","span","groups"}], "pattern"}。

    参数：pattern=正则（支持 Python 命名分组 (?P<name>...)；疑似灾难性回溯的写法会被
    拒绝并说明原因）；text=目标文本（长度受限）；flags_ignorecase=忽略大小写；
    limit=最多条数（默认 50，上限 200）。"""
    return tools.regex_find(pattern, text, flags_ignorecase, limit)


@mcp.tool()
def text_stats(text: str) -> dict:
    """文本统计，返回 chars/chars_no_space/words/lines 四项计数。"""
    return tools.text_stats(text)


def main() -> None:
    run_server(mcp, description="【下层】通用工具 MCP server", default_port=9100)


if __name__ == "__main__":
    main()
