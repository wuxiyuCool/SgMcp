"""端到端冒烟测试（协议级）：用官方 SDK v2 的 Client 验证三层链路。

与旧版「直接调 Python 函数」不同，本测试让调用真实经过 MCP 协议层
（工具 schema 生成、JSON 序列化、结果解包），更接近客户端真实行为：

1. 下层 common：echo / slugify / now（含 Asia/Shanghai 时区）。
2. 中层 it_ops：工单创建与查询。
3. 上层 gateway：**聚合器架构**端到端——进程内起 it_ops / common 真实
   Streamable HTTP server，网关作为 MCP Client 拉取 tools/list 合并工具表，
   再经统一入口完成审批闸门完整流（只读放行 / 高风险挂起 / 批准后经 HTTP
   转发执行 / 驳回不执行 / 未注册路由报错 / 聚合一致性）。
4. shared.config：platform.env 加载与优先级。

运行（任一装有 mcp>=2.2 的环境，如项目根 .venv）：
    python tests/smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
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


async def _list_tools(server_obj) -> set[str]:
    async with Client(server_obj, raise_exceptions=True) as client:
        result = await client.list_tools()
    return {t.name for t in result.tools}


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

    r = asyncio.run(_call(itops.mcp, "itops.create_incident", {"title": "服务器宕机", "priority": "high"}))
    assert not r.is_error and "INC-" in r.content[0].text

    r = asyncio.run(_call(itops.mcp, "itops.list_incidents", {}))
    assert not r.is_error
    print("PASS  中层·it_ops（协议级，itops.* 前缀）")


# ---------------------------------------------------------------------------
# 网关聚合器：进程内起真实 HTTP 下游，验证「tools/list 聚合 + 路由转发 + 审批」
# ---------------------------------------------------------------------------
def _start_http_server(mcp_obj):
    """在后台线程用 uvicorn 起一个真实 Streamable HTTP server（随机端口）。

    返回 (uvicorn.Server, 实际端口)。用完置 should_exit=True 并 join 即停。
    """
    import uvicorn

    app = mcp_obj.streamable_http_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("HTTP server 启动超时")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, port


def test_gateway_aggregation_flow() -> None:
    import mcp_common_server.main as common
    import mcp_itops.main as itops

    # 1) 起真实下游（it_ops + common，进程内 HTTP）
    itops_srv, itops_th, itops_port = _start_http_server(itops.mcp)
    common_srv, common_th, common_port = _start_http_server(common.mcp)
    # 显式指定下游注册表（不含 go_datahub → 无重试等待）；发现窗口设 0（server 已就绪）
    os.environ["MCP_DOWNSTREAMS"] = (
        f"it_ops=http://127.0.0.1:{itops_port}/mcp,"
        f"common-tools=http://127.0.0.1:{common_port}/mcp"
    )
    os.environ["MCP_DISCOVERY_RETRY_SECONDS"] = "0"

    try:
        # 2) 导入网关（聚合器在导入期读下游注册表）并触发启动聚合
        import mcp_gateway.main as gw

        report = gw.aggregator.sync()
        assert set(report.ok) == {"it_ops", "common-tools"}, f"聚合失败: {report}"
        assert report.ok["it_ops"] == len(asyncio.run(_list_tools(itops.mcp)))
        assert report.ok["common-tools"] == len(asyncio.run(_list_tools(common.mcp)))
        print(f"PASS  网关·聚合器 tools/list 合并（{report.ok}）")

        async def flow() -> None:
            async with Client(gw.mcp, raise_exceptions=True) as client:
                # 3) 工具发现：list_routes == 两个下游 tools/list 的并集（含入参 schema）
                r = await client.call_tool("list_routes", {})
                data = _payload(r)
                items = data["result"] if isinstance(data, dict) and set(data) == {"result"} else data
                pairs = {(x["server"], x["tool"]) for x in items}
                expect = {("it_ops", n) for n in await _list_tools(itops.mcp)} | {
                    ("common-tools", n) for n in await _list_tools(common.mcp)
                }
                assert pairs == expect, f"聚合路由与下游不一致: 多={pairs - expect} 少={expect - pairs}"
                assert all(x.get("input_schema") for x in items), "聚合路由应携带入参 schema"
                print(f"PASS  网关·list_routes 聚合一致性（{len(pairs)} 条路由）")

                # 4) 只读工具直接放行（经 HTTP 转发到 common）
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "common-tools", "tool": "echo", "arguments": {"message": "ping"}},
                )
                d = _payload(r)
                assert d["approved"] is True and d["executed"] is True and d["result"] == "ping"
                print("PASS  网关·只读工具直接放行（HTTP 转发）")

                # 5) 无需审批的写工具：经 HTTP 转发执行到 it_ops
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops.create_incident",
                     "arguments": {"title": "聚合器冒烟", "priority": "high"}},
                )
                d = _payload(r)
                assert d["executed"] is True and "INC-" in str(d["result"]), d
                print("PASS  网关·写工具经 HTTP 转发执行")

                # 6) 高风险写操作：挂起，创建审批单，不执行
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops.create_change", "arguments": {"title": "升级库", "risk": "high"}},
                )
                d = _payload(r)
                assert d["approved"] is False and d["executed"] is False
                rid = d["request_id"]
                print(f"PASS  网关·高风险写操作挂起（request_id={rid}）")

                # 7) 批准后才真正执行（HTTP 转发到 it_ops）
                r = await client.call_tool("approve_request", {"request_id": rid})
                d = _payload(r)
                assert d["executed"] is True and "CHG-" in str(d["result"])
                print("PASS  网关·批准后经 HTTP 转发执行")

                # 8) 驳回：不执行
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops.create_change", "arguments": {"title": "再试一次"}},
                )
                rid2 = _payload(r)["request_id"]
                r = await client.call_tool("reject_request", {"request_id": rid2, "comment": "风险过高"})
                d = _payload(r)
                assert d["executed"] is False
                print("PASS  网关·驳回后不执行")

                # 9) 未注册路由：报 ToolError，且错误信息引导工具发现 / 运行时补拉
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "nope", "tool": "x", "arguments": {}},
                )
                assert r.is_error
                assert "list_routes" in str(r.content), "未注册路由应引导调用 list_routes"
                print("PASS  网关·未注册路由返回协议错误（含发现引导）")

                # 10) refresh_routes：运行时重拉工具表
                r = await client.call_tool("refresh_routes", {})
                d = _payload(r)
                assert set(d["ok"]) == {"it_ops", "common-tools"} and not d["failed"], d
                print("PASS  网关·refresh_routes 运行时补拉")

        asyncio.run(flow())
    finally:
        for srv, th in ((itops_srv, itops_th), (common_srv, common_th)):
            srv.should_exit = True
            th.join(timeout=5)
        os.environ.pop("MCP_DOWNSTREAMS", None)
        os.environ.pop("MCP_DISCOVERY_RETRY_SECONDS", None)


def test_shared_config() -> None:
    """统一敏感配置入口：文件加载、env 优先、路径覆盖。"""
    import tempfile

    from mcp_shared import config

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "platform.env"
        f.write_text('# 注释\nexport DEMO_SECRET=from-file\nDEMO_URL="http://x/y"\n坏行没有等号\n', encoding="utf-8")
        os.environ["MCP_CONFIG_FILE"] = str(f)
        os.environ.pop("DEMO_SECRET", None)
        os.environ.pop("DEMO_URL", None)
        try:
            config._reset()
            config.load_platform_env()
            assert os.environ.get("DEMO_SECRET") == "from-file", os.environ.get("DEMO_SECRET")
            assert config.get("DEMO_URL") == "http://x/y"
            os.environ["DEMO_SECRET"] = "from-env"
            assert config.get("DEMO_SECRET") == "from-env", "OS 环境变量应优先于配置文件"
        finally:
            os.environ.pop("DEMO_SECRET", None)
            os.environ.pop("DEMO_URL", None)
            os.environ.pop("MCP_CONFIG_FILE", None)
            config._reset()
    print("PASS  shared·config platform.env（文件加载 + env 优先）")


def test_shared_dsrouting() -> None:
    """业务名路由（Python 侧）：租户优先/兜底、按月分表、未配置报可读错误。"""
    from mcp_shared import dsrouting

    os.environ["DATASET_ORDERS_DBTYPE"] = "pg"
    os.environ["DATASET_ORDERS_DSN_T1"] = "order_t1_pg"
    os.environ["DATASET_ORDERS_DSN_DEFAULT"] = "order_pg"
    try:
        assert dsrouting.route_db("orders", "t1") == ("pg", "order_t1_pg")
        assert dsrouting.route_db("ORDERS", "T9") == ("pg", "order_pg")  # 兜底 + 大小写归一
        assert dsrouting.route_table("orders", "2024-05") == "orders_202405"
        for bad in ("2024-13", "202405", "'; DROP TABLE x"):
            try:
                dsrouting.route_table("orders", bad)
                raise AssertionError(f"非法 period 竟通过: {bad}")
            except ValueError:
                pass
        try:
            dsrouting.route_db("no_such_ds", "t1")
            raise AssertionError("未配置数据集竟通过")
        except ValueError as e:
            assert "DATASET_NO_SUCH_DS_DSN_DEFAULT" in str(e)
    finally:
        for k in ("DATASET_ORDERS_DBTYPE", "DATASET_ORDERS_DSN_T1", "DATASET_ORDERS_DSN_DEFAULT"):
            os.environ.pop(k, None)
    print("PASS  shared·dsrouting 业务名路由（租户/兜底/按月分表）")


if __name__ == "__main__":
    test_lower_common()
    test_middle_itops()
    test_gateway_aggregation_flow()
    test_shared_config()
    test_shared_dsrouting()
    print("\n全部冒烟测试通过 ✅")
