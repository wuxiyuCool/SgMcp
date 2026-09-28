"""【下层】通用工具 MCP server 入口。

提供与业务无关的通用工具（实现见 tools.py），是三层中最底层的基础服务。

规范（docs/develop-deploy.md「工具清单」）：
- 命名 ``util_{action}_{resource}``，动词取白名单（get/create/search/calc），
  全域 8 个（≤8 治理线），全局唯一、永久稳定、不含动态元素（WeKnora 兼容）；
- description 写明「Use when / Do NOT use when」；错误带 ``[COMMON_<CODE>]`` 前缀；
- 入参枚举一律用 `Literal`——生成 JSON Schema enum 让 AI 第一次就填对，
  比运行时报错省一轮往返。

运行：
- stdio：  common-server
- HTTP：   common-server --transport http --port 9100
"""

from __future__ import annotations

from typing import Literal

from mcp.server import MCPServer
from mcp_types import ToolAnnotations

from mcp_common_server import tools
from mcp_shared import run_server

mcp = MCPServer("common-tools")

# 全部为无外部副作用的纯计算：只读。util_create_id 结果随机，非幂等。
_RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                      open_world_hint=False)
_RO_RANDOM = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=False,
                             open_world_hint=False)


@mcp.tool(name="util_get_time", title="当前时间与时区换算", annotations=_RO)
def util_get_time(timezone_name: str = "Asia/Shanghai", value: str = "",
                  from_tz: str = "UTC", offset_days: int = 0, offset_hours: int = 0) -> dict:
    """取当前时间，或做时区换算/时间偏移（value 留空=当前时间）。

    Use when：需要"现在几点"、把某个 ISO 时间换算到别的时区、算相对时间
    （如"3 天前到现在"用 offset_days=-3）。返回 converted/shifted 两个 ISO 串。
    Do NOT use when：查业务单据里的时间（工单创建时间等）——那是平台数据本身。
    参数：timezone_name=IANA 时区名（任意合法名，如 Asia/Shanghai）；
    value=ISO 8601 时间串（留空取当前时间；也接受 "2026-09-24" 与 "Z" 后缀）；
    from_tz=无时区标记的输入按此时区解释；offset_days/offset_hours=偏移（可负）。
    """
    return tools.get_time(timezone_name, value, from_tz, offset_days, offset_hours)


@mcp.tool(name="util_create_id", title="生成 ID / 随机串", annotations=_RO_RANDOM)
def util_create_id(kind: Literal["prefixed", "uuid", "random"] = "prefixed", count: int = 1,
                   prefix: str = "id", version: Literal[4, 7] = 4, length: int = 16,
                   charset: Literal["alnum", "alpha", "hex", "digits"] = "alnum",
                   digits_only: bool = False) -> dict:
    """批量生成唯一 ID / 随机串，返回 {"ids": [...]}（count 上限 100）。

    Use when：需要幂等键（经网关 gateway_call 的 idempotency_key 传回，让写操作
    重试安全）、追踪号、临时密码/验证码。kind=prefixed 带前缀短 ID；uuid 标准
    UUID（4 随机/7 时间有序）；random 密码学随机串（charset 选字符集，
    digits_only 纯数字）。
    Do NOT use when：需要业务单号（由平台系统自己生成，如 ITOM 的 eventNo）。
    每次调用结果必然不同，生成后请原样复用，不要指望两次调用返回同值。
    """
    return tools.create_id(kind, count, prefix, version, length, charset, digits_only)


@mcp.tool(name="util_get_encoded", title="摘要 / Base64 / slug", annotations=_RO)
def util_get_encoded(text: str,
                     algo: Literal["md5", "sha1", "sha256", "sha512", "base64", "slug"] = "sha256",
                     mode: Literal["encode", "decode"] = "encode") -> dict:
    """文本摘要与编解码统一入口：md5/sha1/sha256/sha512 返回 {"algo","hex"}，
    base64/slug 返回 {"algo","result"}。

    Use when：算校验哈希、Base64 编解码（mode=encode/decode）、把标题转成
    文件名/URL slug。
    Do NOT use when：加密敏感数据——这些都不是加密；口令类比较请走业务系统。
    注意：slug 只保留字母数字，中文会被整体去除（结果可能为空串）。
    """
    return tools.get_encoded(text, algo, mode)


@mcp.tool(name="util_get_json", title="JSON 校验与变换", annotations=_RO)
def util_get_json(text: str,
                  mode: Literal["validate", "pretty", "minify", "keys"] = "validate") -> dict:
    """JSON 处理：validate（合法性+错误位置）/pretty（缩进）/minify（压缩）/keys（顶层键）。

    Use when：校验 AI 拼装或外部来的 JSON、格式化展示前压缩、看顶层结构。
    Do NOT use when：解析 YAML/XML——本工具只认 JSON。
    validate 对非法 JSON 也正常返回 {"valid": false, "error", "line", "column",
    "hint"}，不报工具错误——直接看错在哪一行，不要盲目重试。
    """
    return tools.get_json(text, mode)


@mcp.tool(name="util_get_url", title="拆解 URL", annotations=_RO)
def util_get_url(url: str) -> dict:
    """拆解 URL，返回 scheme/host/port/path/query（参数字典）/fragment。

    Use when：从回调/ webhook 地址里取 host、path 或某个查询参数。
    Do NOT use when：请求这个 URL——本工具不发起网络访问，只解析字符串。
    """
    return tools.get_url(url)


@mcp.tool(name="util_search_text", title="正则搜索", annotations=_RO)
def util_search_text(pattern: str, text: str, flags_ignorecase: bool = False,
                     limit: int = 50) -> dict:
    """正则搜索，返回 {"count", "matches": [{"text","span","groups"}], "pattern"}。

    Use when：在一段文本里批量提取符合模式的内容（单号、IP、编码等）。
    支持 Python 命名分组 (?P<name>...)，分组结果在 groups 里。
    Do NOT use when：全文语义检索——那是知识库的事；疑似灾难性回溯的正则
    （嵌套量词等）会被拒绝并说明原因。
    参数：flags_ignorecase=忽略大小写；limit=最多条数（默认 50，上限 200）。
    """
    return tools.search_text(pattern, text, flags_ignorecase, limit)


@mcp.tool(name="util_calc_stats", title="文本字数统计", annotations=_RO)
def util_calc_stats(text: str) -> dict:
    """文本统计，返回 chars/chars_no_space/words/lines 四项计数。

    Use when：估算一段文本的规模（是否超接口长度限制、要不要截断）。
    Do NOT use when：统计 token 数——words 不等于 tokens，只能做量级参考。
    """
    return tools.calc_stats(text)


@mcp.tool(name="util_get_status", title="下层服务自检", annotations=_RO)
def util_get_status() -> dict:
    """下层自检：各 util_* 工具跑一遍并回报规模上限配置。

    Use when：部署完确认服务健康、排障时判断下层是否可用。
    Do NOT use when：查 ITOM 对接状态——那是 itops_get_itom_status。
    返回 {"ok": true, "checks": {...}, "limits": {...}}；任一项失败 ok=false
    且 checks 里给出该项错误。运维巡检与 CI 起步用。
    """
    return tools.self_check()


def main() -> None:
    run_server(mcp, description="【下层】通用工具 MCP server", default_port=9100)


if __name__ == "__main__":
    main()
