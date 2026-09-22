"""端到端冒烟测试（协议级）：用官方 SDK v2 的内存 Client 直连各层 server。

与旧版「直接调 Python 函数」不同，本测试让调用真实经过 MCP 协议层
（工具 schema 生成、JSON 序列化、结果解包），更接近客户端真实行为：

1. 下层 common：echo / slugify / now（含 Asia/Shanghai 时区）。
2. 中层 it_ops：工单创建与查询。
3. 上层 gateway：统一入口 + 审批闸门完整流（只读放行 / 高风险挂起 /
   批准后执行 / 驳回不执行 / 未注册路由报错）。

运行（任一装有 mcp>=2.2 的环境，如项目根 .venv）：
    python tests/smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 开发期目录结构：未 pip install 时也能 import 各包（editable 安装后此段无副作用）
ROOT = Path(__file__).resolve().parents[1]
for sub in (
    "shared/src",
    "layers/common/src",
    "layers/business/it_ops/src",
    "layers/gateway/src",
):
    sys.path.insert(0, str(ROOT / sub))

from mcp import Client  # noqa: E402


async def _call(server_obj, tool: str, args: dict):
    async with Client(server_obj, raise_exceptions=True) as client:
        return await client.call_tool(tool, args)


def _payload(result) -> dict:
    """取工具返回的 JSON 负载（优先结构化输出，退回文本内容）。"""
    assert not result.is_error, f"工具返回错误: {result.content}"
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def test_lower_common() -> None:
    from mcp_common_server import main as common

    r = asyncio.run(_call(common.mcp, "echo", {"message": "ping"}))
    assert not r.is_error and r.content[0].text == "ping"

    r = asyncio.run(_call(common.mcp, "slugify", {"text": "Hello World!"}))
    assert r.content[0].text == "hello-world"

    r = asyncio.run(_call(common.mcp, "now", {"timezone_name": "Asia/Shanghai"}))
    assert not r.is_error and "+08:00" in r.content[0].text
    print("PASS  下层·通用工具（协议级）")


def test_middle_itops() -> None:
    from mcp_itops import main as itops

    r = asyncio.run(_call(itops.mcp, "create_incident", {"title": "服务器宕机", "priority": "high"}))
    assert not r.is_error and "INC-" in r.content[0].text

    r = asyncio.run(_call(itops.mcp, "list_incidents", {}))
    assert not r.is_error
    print("PASS  中层·it_ops（协议级）")


def test_gateway_approval_flow() -> None:
    import mcp_gateway.main as gw

    async def flow() -> None:
        async with Client(gw.mcp, raise_exceptions=True) as client:
            # 1) 只读工具直接放行
            r = await client.call_tool(
                "gateway_call",
                {"server": "common-tools", "tool": "echo", "arguments": {"message": "ping"}},
            )
            d = _payload(r)
            assert d["approved"] is True and d["executed"] is True and d["result"] == "ping"
            print("PASS  网关·只读工具直接放行")

            # 2) 高风险写操作：挂起，创建审批单，不执行
            r = await client.call_tool(
                "gateway_call",
                {"server": "it_ops", "tool": "create_change", "arguments": {"title": "升级库", "risk": "high"}},
            )
            d = _payload(r)
            assert d["approved"] is False and d["executed"] is False
            rid = d["request_id"]
            print(f"PASS  网关·高风险写操作挂起（request_id={rid}）")

            # 3) 批准后才真正执行下游调用
            r = await client.call_tool("approve_request", {"request_id": rid})
            d = _payload(r)
            assert d["executed"] is True and "CHG-" in str(d["result"])
            print("PASS  网关·批准后执行下游调用")

            # 4) 驳回：不执行
            r = await client.call_tool(
                "gateway_call",
                {"server": "it_ops", "tool": "create_change", "arguments": {"title": "再试一次"}},
            )
            rid2 = _payload(r)["request_id"]
            r = await client.call_tool("reject_request", {"request_id": rid2, "comment": "风险过高"})
            d = _payload(r)
            assert d["executed"] is False
            print("PASS  网关·驳回后不执行")

            # 5) 未注册路由：报 ToolError（is_error 结果）
            r = await client.call_tool(
                "gateway_call",
                {"server": "nope", "tool": "x", "arguments": {}},
            )
            assert r.is_error
            print("PASS  网关·未注册路由返回协议错误")

    asyncio.run(flow())


if __name__ == "__main__":
    test_lower_common()
    test_middle_itops()
    test_gateway_approval_flow()
    print("\n全部冒烟测试通过 ✅")
