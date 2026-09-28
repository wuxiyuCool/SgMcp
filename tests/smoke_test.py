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

# it_ops SQLite 与审计/审批台账隔离到系统临时目录（避免冒烟污染仓库）
import tempfile  # noqa: E402

_TMP = Path(tempfile.gettempdir()) / "sgmcp_smoke"
_TMP.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_ITOPS_DB", str(_TMP / "itops.db"))
os.environ.setdefault("MCP_AUDIT_DIR", str(_TMP / "audit"))
os.environ.setdefault("MCP_APPROVAL_STORE", str(_TMP / "audit" / "approvals.jsonl"))

# 数据集路由与域归属（与 scripts/ops-check.ps1 同一套键）：it_ops 的领域枚举与
# 中文说明在导入期由这些配置生成，冒烟必须在 import 前放好
os.environ.setdefault("DATASET_INCIDENTS_DBTYPE", "pg")
os.environ.setdefault("DATASET_INCIDENTS_DSN_DEFAULT", "itsm_pg")
os.environ.setdefault("DATASET_INCIDENTS_DOMAIN", "itops")
os.environ.setdefault("DATASET_INCIDENTS_DESC", "IT 运维事件单（工单流水）")
os.environ.setdefault("DATASET_ASSETS_DSN_DEFAULT", "itsm_pg")
os.environ.setdefault("DATASET_ASSETS_DOMAIN", "itops")
os.environ.setdefault("DATASET_ASSETS_DESC", "CMDB 资产台账")


async def _call(server_obj, tool: str, args: dict):
    async with Client(server_obj, raise_exceptions=True) as client:
        return await client.call_tool(tool, args)


async def _list_tools(server_obj) -> set[str]:
    async with Client(server_obj, raise_exceptions=True) as client:
        result = await client.list_tools()
    return {t.name for t in result.tools}


def _items(payload):
    """剥掉 list 返回值的 {"result": [...]} 信封。"""
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


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

    r = asyncio.run(_call(itops.mcp, "itops_create_incident", {"title": "服务器宕机", "priority": "high"}))
    assert not r.is_error and "INC-" in r.content[0].text

    r = asyncio.run(_call(itops.mcp, "itops_list_incidents", {}))
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
                assert all("params" in x for x in items), "聚合路由应携带入参摘要（无参工具为空对象）"
                assert any(x["params"] for x in items), "有参工具的摘要不能为空"
                full = _payload(await client.call_tool("list_routes", {"full_schema": True}))
                full = full["result"] if isinstance(full, dict) and set(full) == {"result"} else full
                assert all(x.get("input_schema") for x in full), "full_schema=True 才回完整 JSON Schema"
                filtered = _payload(await client.call_tool("list_routes", {"server": "Common-Tools"}))
                filtered = filtered["result"] if isinstance(filtered, dict) else filtered
                assert filtered and all(x["server"] == "common-tools" for x in filtered), "server 过滤生效"
                print(f"PASS  网关·list_routes 聚合一致性（{len(pairs)} 条路由，摘要/完整/过滤三态）")

                # 4) 只读工具直接放行（经 HTTP 转发到 common）
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "common-tools", "tool": "echo", "arguments": {"message": "ping"}},
                )
                d = _payload(r)
                assert d["approved"] is True and d["executed"] is True and d["result"] == "ping"
                print("PASS  网关·只读工具直接放行（HTTP 转发）")

                # 4b) arguments 以 JSON 字符串传输（部分 AI 平台会序列化嵌套对象）→ 容错生效
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "common-tools", "tool": "echo",
                     "arguments": '{"message": "str-args"}'},
                )
                d = _payload(r)
                assert d["executed"] is True and d["result"] == "str-args", d
                print("PASS  网关·arguments 兼容 JSON 字符串形式")

                # 4c) 路由宽容解析：脏 server（大小写/连字符）与漏层级前缀的 tool 都能救回
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "Common-Tools", "tool": "echo", "arguments": {"message": "sloppy"}},
                )
                assert _payload(r)["result"] == "sloppy"
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "create_incident",
                     "arguments": {"title": "漏前缀容错"}},
                )
                d = _payload(r)
                assert d["executed"] is True and "INC-" in str(d["result"]), d
                print("PASS  网关·server/tool 宽容解析（归一化+补层级前缀）")

                # 4d) 入口精简：gateway_call 直接吃扁平 kv 串（原 gateway_call_kv 已并入）
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "common-tools", "tool": "echo", "arguments": "message=kv-args;uppercase=true"},
                )
                d = _payload(r)
                assert d["executed"] is True and d["result"] == "KV-ARGS", d
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "create_incident", "arguments": "title=kv入口工单;priority=high"},
                )
                d = _payload(r)
                assert d["executed"] is True and "INC-" in str(d["result"]), d
                # 下划线别名（规避平台对参数值中 "." 的序列化缺陷）：itops_create_incident
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops_create_incident", "arguments": "title=别名调用"},
                )
                d = _payload(r)
                assert d["executed"] is True and "INC-" in str(d["result"]), d
                # list_routes：工具名一律无点号（规避平台 "." 序列化缺陷）+ 携带 tool_alias
                r = await client.call_tool("list_routes", {})
                items = _payload(r)
                items = items["result"] if isinstance(items, dict) and set(items) == {"result"} else items
                assert all("." not in x["tool"] for x in items), [x["tool"] for x in items if "." in x["tool"]]
                assert all(x["tool_alias"] == x["tool"] for x in items)
                assert any(x["tool"].startswith("itops_") for x in items)
                print("PASS  网关·gateway_call 统一入口（对象/JSON/扁平 kv 三形态）+ 别名")

                # 4e) 聚合路由工具 route_*：每 server 一个入口，method 枚举 == 该 server 全部工具
                r = await client.list_tools()
                gw_tools = {t.name: t for t in r.tools}
                assert "gateway_call_kv" not in gw_tools, "重复入口 gateway_call_kv 应已摘除"
                route_names = {n for n in gw_tools if n.startswith("route_")}
                assert route_names == {"route_it_ops", "route_common_tools"}, route_names
                itops_tools = await _list_tools(itops.mcp)
                enum_itops = set(gw_tools["route_it_ops"].input_schema["properties"]["method"]["enum"])
                assert enum_itops == itops_tools, f"枚举与下游不一致: {enum_itops ^ itops_tools}"
                assert "itops_create_change" in gw_tools["route_it_ops"].description, "描述须列出审批方法"
                # 经 route 工具直接调用（kv 扁平串入参）
                r = await client.call_tool(
                    "route_common_tools", {"method": "echo", "params": "message=route-fn;uppercase=true"}
                )
                d = _payload(r)
                assert d["executed"] is True and d["result"] == "ROUTE-FN", d
                # 经 route 工具触发审批闸门
                r = await client.call_tool(
                    "route_it_ops", {"method": "itops_create_change", "params": {"title": "route审批", "risk": "high"}}
                )
                d = _payload(r)
                assert d["approved"] is False and d["executed"] is False and d["request_id"].startswith("apr_"), d
                r = await client.call_tool("reject_request", {"request_id": d["request_id"]})
                assert _payload(r)["executed"] is False
                # 非法 method：可读错误并列出枚举
                r = await client.call_tool("route_common_tools", {"method": "no_such", "params": {}})
                assert r.is_error and "no_such" in str(r.content)
                print("PASS  网关·route_* 聚合路由工具（枚举一致+kv 入参+审批闸门+错误引导）")

                # 4f) list_tool_catalog：全目录（工具在哪个 MCP + 上层如何传递调用）
                r = await client.call_tool("list_tool_catalog", {"mode": "full"})
                groups = _payload(r)
                groups = groups["result"] if isinstance(groups, dict) and set(groups) == {"result"} else groups
                gmap = {g["mcp_server"]: g for g in groups}
                assert set(gmap) == {"it_ops", "common-tools"}, gmap.keys()
                for srv, g in gmap.items():
                    assert g["endpoint"] and g["route_tool"] in gw_tools, f"{srv} 目录组缺 endpoint/route_tool"
                    assert {t["tool"] for t in g["tools"]} == (
                        await _list_tools(itops.mcp) if srv == "it_ops" else await _list_tools(common.mcp)
                    ), f"{srv} 目录与下游工具表不一致"
                    assert "Streamable HTTP 转发" in g["call_chain"]
                    for t in g["tools"]:
                        assert t["in_mcp"] == srv and "method" in t["example"], t
                echo_entry = next(t for t in gmap["common-tools"]["tools"] if t["tool"] == "echo")
                assert echo_entry["params"]["message"]["required"] is True
                assert echo_entry["params"]["uppercase"]["type"] == "boolean"
                print("PASS  网关·list_tool_catalog（MCP 归属 + 调用链 + 参数说明书）")

                # 5) 无需审批的写工具：经 HTTP 转发执行到 it_ops
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops_create_incident",
                     "arguments": {"title": "聚合器冒烟", "priority": "high"}},
                )
                d = _payload(r)
                assert d["executed"] is True and "INC-" in str(d["result"]), d
                print("PASS  网关·写工具经 HTTP 转发执行")

                # 6) 高风险写操作：挂起，创建审批单，不执行
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops_create_change", "arguments": {"title": "升级库", "risk": "high"}},
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
                    {"server": "it_ops", "tool": "itops_create_change", "arguments": {"title": "再试一次"}},
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

                # 11) route_* 的 method 是 enum：拼错的方法直接被 schema 拦下（不再宽容猜）
                props = gw_tools["route_it_ops"].input_schema["properties"]
                assert props["method"].get("enum"), "method 必须带枚举"
                assert "object" in json.dumps(props["params"]), "params 应声明对象形态"
                r = await client.call_tool("route_it_ops", {"method": "not_a_tool", "params": {}})
                assert r.is_error, "枚举外的 method 竟然通过"
                print("PASS  网关·method 枚举约束（拼错即拦 + 参数类型可见）")

                # 12) 工具名写错时给相近候选（省 AI 往返）
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "create_inciden", "arguments": {"title": "typo"}},
                )
                assert r.is_error and "itops_create_incident" in str(r.content), \
                    f"未命中时应给出相近工具名: {r.content}"
                print("PASS  网关·未命中路由给相近工具名")

                # 13) 审批令牌：配了 MCP_APPROVAL_TOKEN 后，AI 侧无令牌无法自批
                os.environ["MCP_APPROVAL_TOKEN"] = "human-only-secret"
                try:
                    r = await client.call_tool(
                        "gateway_call",
                        {"server": "it_ops", "tool": "itops_create_change",
                         "arguments": {"title": "令牌保护"}},
                    )
                    rid3 = _payload(r)["request_id"]
                    r = await client.call_tool("approve_request", {"request_id": rid3})
                    assert r.is_error and "审批令牌" in str(r.content), \
                        f"无令牌的 approve_request 竟然放行: {r.content}"
                    r = await client.call_tool(
                        "approve_request", {"request_id": rid3, "approval_token": "human-only-secret"}
                    )
                    assert _payload(r)["executed"] is True, "带令牌的审批人应能放行"
                    print("PASS  网关·审批令牌挡住 AI 自批（人类令牌才能放行）")
                finally:
                    os.environ.pop("MCP_APPROVAL_TOKEN", None)

                # 14) 批准后下游执行失败 → 单据置 exec_failed（可重放），不静默丢单
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops_create_change",
                     "arguments": {"title": "超长标题" + "呀" * 600}},
                )
                rid4 = _payload(r)["request_id"]
                r = await client.call_tool("approve_request", {"request_id": rid4})
                assert r.is_error, "下游必然失败的场景竟然执行成功"
                st = _payload(await client.call_tool("list_approvals",
                                                     {"request_id_filter": rid4})) \
                    if False else None
                r = await client.call_tool("list_approvals", {"status": "exec_failed"})
                failed_ids = [x["id"] for x in _items(_payload(r))]
                assert rid4 in failed_ids, f"执行失败的审批单应可查（exec_failed）: {failed_ids}"
                assert "attempts" in next(x for x in _items(_payload(r)) if x["id"] == rid4)
                print("PASS  网关·批准但下游失败 → exec_failed 可追溯/可重放")

                # 15) 幂等键：同一 key 的重复写请求不会二次执行（AI 重试安全）
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops_create_incident",
                     "arguments": {"title": "幂等验证"}, "idempotency_key": "smoke-key-1"},
                )
                first = _payload(r)["result"]["id"]
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops_create_incident",
                     "arguments": {"title": "幂等验证"}, "idempotency_key": "smoke-key-1"},
                )
                again = _payload(r)
                assert again["result"]["id"] == first and again.get("idempotent_replay") is True, again
                print("PASS  网关·idempotency_key 幂等重放（重试不重复建单）")

                # 16) 结果规模上限：超限截断并引导改用过滤/异步（护住上下文与 token）
                gw.MAX_RESULT_ITEMS = 3
                try:
                    for i in range(6):
                        await client.call_tool(
                            "gateway_call",
                            {"server": "it_ops", "tool": "itops_create_incident",
                             "arguments": {"title": f"批量{i}", "priority": "low"}},
                        )
                    r = await client.call_tool(
                        "gateway_call",
                        {"server": "it_ops", "tool": "itops_list_incidents", "arguments": {}},
                    )
                    capped = _payload(r)["result"]
                    assert isinstance(capped, dict) and capped["truncated"] is True, capped
                    assert capped["returned"] == 3 and capped["total"] >= 6, capped
                    assert "data_submit_collect_job" in capped["note"] or "过滤" in capped["note"]
                finally:
                    gw.MAX_RESULT_ITEMS = 200
                print("PASS  网关·结果超限截断（返回条数/总数/引导）")

                # 17) 扁平 kv 串的数值语义：前导零编号与版本号不被改坏
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops_create_incident",
                     "arguments": "title='编号 007 与版本 1.10';priority=high"},
                )
                d = _payload(r)
                assert d["executed"] is True and "007" in str(d["result"]["title"]), d
                print("PASS  网关·kv 引号保原样（007/1.10 不被误转数值）")

                # 18) 审计落盘：调用与审批可回查，敏感入参已脱敏
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "common-tools", "tool": "echo",
                     "arguments": {"message": "hi", "token": "super-secret-value"}},
                )
                _payload(r)
                r = await client.call_tool("query_audit_log", {"limit": 50})
                events = _items(_payload(r))
                assert any(e["event"] == "tool_call" for e in events), events[:2]
                assert any(e["event"] in {"approval_created", "approval_decided"} for e in events)
                leaked = [e for e in events if "super-secret-value" in json.dumps(e, ensure_ascii=False)]
                assert not leaked, f"审计日志泄露敏感入参: {leaked}"
                echoed = next(e for e in events if e.get("tool") == "echo" and "args" in e)
                assert echoed["args"]["token"] == "***", echoed
                print(f"PASS  网关·审计落盘（{len(events)} 条事件，敏感键已遮蔽）")

                # 19) describe_gateway：部署自检（安全开关一目了然）
                r = await client.call_tool("describe_gateway", {})
                info = _payload(r)
                assert info["routes"] > 0
                assert info["security"]["approval_token_required"] is False, "测试环境未配令牌应为 False"
                assert "gateway_bearer_enabled" in info["security"]
                print(f"PASS  网关·describe_gateway 自检（{info['routes']} 条路由，安全开关可见）")

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
        assert dsrouting.route_db("orders", "default") == ("pg", "order_pg"), \
            "tenant_id=default 是各工具的默认值，应走兜底路由而不是报错"
        for bad_tenant in ("t1;DATASET_ORDERS_DSN_T1", "a b", "../x"):
            try:
                dsrouting.route_db("orders", bad_tenant)
                raise AssertionError(f"非法租户名竟通过: {bad_tenant}")
            except ValueError:
                pass  # 租户名参与拼 env 键，必须限字符集（防配置注入）
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


def test_itops_persistence() -> None:
    """SQLite store：写入后用"新进程"重开同一库文件，记录仍在（跨重启持久化）。"""
    from mcp_itops.store import SqliteStore, local_store_available

    if not local_store_available():
        print("SKIP  it_ops·SQLite 跨重启持久化（该解释器未编译 sqlite3）")
        return
    db = os.environ["MCP_ITOPS_DB"]
    s1 = SqliteStore(db)
    rec = s1.create_incident(title="持久化验证", priority="low", reporter="smoke")
    s1.update_incident_status(rec["id"], "in_progress")
    s2 = SqliteStore(db)  # 模拟服务重启后的全新实例
    found = [i for i in s2.list_incidents(status="in_progress") if i["id"] == rec["id"]]
    assert found and found[0]["title"] == "持久化验证", found
    print(f"PASS  it_ops·SQLite 跨重启持久化（{rec['id']} 重开库仍可查）")


def test_itops_degrades_without_sqlite() -> None:
    """解释器缺 _sqlite3 时 it_ops 必须照常启动：只有 local 通道报可读错误，
    api/sql 通道不受影响（否则一个 stdlib 扩展缺失会拖垮整个下游聚合）。"""
    import importlib

    from mcp_itops import store as itops_store

    real = itops_store.sqlite3
    itops_store.sqlite3 = None
    itops_store._store = None
    try:
        # 1) 建记录是纯函数：不依赖 sqlite，api/sql 通道拿得到同样的行
        rec = itops_store.new_incident(title="API 通道工单", priority="high", reporter="smoke")
        assert rec["id"].startswith("INC-") and rec["status"] == "new", rec

        # 2) 模块能重新导入（旧写法在这里直接 ModuleNotFoundError → 服务崩溃循环）
        importlib.reload(importlib.import_module("mcp_itops.main"))
        from mcp_itops import main as itops

        # 3) local 通道 → 引导性错误；api 通道 → 走到 ITSM 配置检查，说明没被 sqlite 卡住
        def expect_error(tool: str, args: dict, needle: str) -> None:
            r = asyncio.run(_call(itops.mcp, tool, args))
            assert r.is_error, f"{tool} {args} 应当报错"
            text = " ".join(c.text for c in r.content)
            assert needle in text, f"{tool} {args}: 期望 {needle}，实际 {text}"

        expect_error("itops_create_incident", {"title": "本地工单"}, "local 通道不可用")

        # api 通道绕开了本地存储：报错只可能来自 ITSM 配置/调用，不该再提 local 通道
        r = asyncio.run(_call(itops.mcp, "itops_create_incident", {"title": "接口工单", "channel": "api"}))
        assert "local 通道不可用" not in " ".join(c.text for c in r.content)

        # 4) 变更单/资产没有别的通道，也必须给可读错误而不是 500
        expect_error("itops_create_change", {"title": "变更"}, "local 通道不可用")
        expect_error("itops_list_assets", {}, "local 通道不可用")
    finally:
        itops_store.sqlite3 = real
        itops_store._store = None
        importlib.reload(importlib.import_module("mcp_itops.main"))
    print("PASS  it_ops·缺 sqlite3 时优雅降级（服务起得来，api/sql 通道照常）")



def test_config_inline_comments() -> None:
    """.env 值里的行内注释必须剥掉：中文注释混进令牌值会让下游聚合全线失败。"""
    import tempfile

    from mcp_shared import config

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "platform.env"
        lines = [
            "TOK_PLAIN=abc123   # 网关到 it_ops 的令牌",
            'TOK_QUOTED="abc123 # 保留"',
            "TOK_SQ='abc123'",
            "TOK_URL=http://127.0.0.1:9300/mcp   # 本机",
            "TOK_BARE=abc123",
        ]
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ["MCP_CONFIG_FILE"] = str(f)
        try:
            config._reset()
            vals = config.load_platform_env()
            assert vals["TOK_PLAIN"] == "abc123", vals
            assert vals["TOK_QUOTED"] == "abc123 # 保留", "引号内的 # 属于值本身"
            assert vals["TOK_SQ"] == "abc123"
            assert vals["TOK_URL"] == "http://127.0.0.1:9300/mcp"
            assert vals["TOK_BARE"] == "abc123"
            # 所有解析出的值都必须能进 HTTP 头（ASCII）
            for k, v in vals.items():
                v.encode("ascii")
        finally:
            os.environ.pop("MCP_CONFIG_FILE", None)
            config._reset()

    # 网关侧：非 ASCII 头值要给出配置错误指引，而不是撞 UnicodeEncodeError
    import mcp_gateway.main as gw
    from mcp_gateway.aggregator import Aggregator, DownstreamSpec

    bad = DownstreamSpec("x", "http://127.0.0.1:1/mcp", {"Authorization": "Bearer 令牌abc"})
    msg = Aggregator.header_config_error(bad)
    assert msg and "config_error" in msg and "非 ASCII" in msg, msg
    assert Aggregator.header_config_error(DownstreamSpec("x", "u", {"Authorization": "Bearer abc"})) is None
    assert Aggregator.header_config_error(DownstreamSpec("x", "u", None)) is None
    print("PASS  config·行内注释剥离 + 非 ASCII 头值给配置错误")


def test_config_inline_comments() -> None:
    """.env 值里的行内注释必须剥掉：中文注释混进令牌值会让下游聚合全线失败。"""
    import tempfile

    from mcp_shared import config

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "platform.env"
        f.write_text(
            'TOK_PLAIN=abc123   # 网关到 it_ops 的令牌\n'
            'TOK_QUOTED="abc123 # 保留"\n'
            "TOK_SQ='abc123'\n"
            "TOK_URL=http://127.0.0.1:9300/mcp   # 本机\n"
            "TOK_BARE=abc123\n",
            encoding="utf-8",
        )
        os.environ["MCP_CONFIG_FILE"] = str(f)
        try:
            config._reset()
            vals = config.load_platform_env()
            assert vals["TOK_PLAIN"] == "abc123", vals
            assert vals["TOK_QUOTED"] == "abc123 # 保留", "引号内的 # 属于值本身"
            assert vals["TOK_SQ"] == "abc123"
            assert vals["TOK_URL"] == "http://127.0.0.1:9300/mcp"
            assert vals["TOK_BARE"] == "abc123"
            for k in ("TOK_PLAIN", "TOK_URL", "TOK_BARE", "TOK_SQ"):
                vals[k].encode("ascii")   # 剥完注释后必须能进 HTTP 头
        finally:
            os.environ.pop("MCP_CONFIG_FILE", None)
            config._reset()

    # 网关侧：非 ASCII 头值要给出配置错误指引，而不是撞 UnicodeEncodeError
    from mcp_gateway.aggregator import Aggregator, DownstreamSpec

    bad = DownstreamSpec("x", "http://127.0.0.1:1/mcp", {"Authorization": "Bearer 令牌abc"})
    msg = Aggregator.header_config_error(bad)
    assert msg and "config_error" in msg and "非 ASCII" in msg, msg
    assert Aggregator.header_config_error(DownstreamSpec("x", "u", {"Authorization": "Bearer abc"})) is None
    assert Aggregator.header_config_error(DownstreamSpec("x", "u", None)) is None
    print("PASS  config·行内注释剥离 + 非 ASCII 头值给配置错误")


def test_shared_limits() -> None:
    """轻活层护栏：文本规模 / ReDoS / 路径围栏（AI 入参不可信的兜底）。"""
    import tempfile
    from pathlib import Path as _P

    from mcp_shared import limits

    os.environ["MCP_MAX_TEXT_CHARS"] = "1000"
    try:
        limits.check_text("x" * 50)
        try:
            limits.check_text("x" * 5000)
            raise AssertionError("超限文本竟然通过")
        except ValueError as e:
            assert "上限" in str(e) and "data_file_stats" in str(e), e
    finally:
        os.environ.pop("MCP_MAX_TEXT_CHARS", None)

    for bad in (r"(a+)+b", "(a|a?)*b"):  # Python re 无超时，一条就能挂死共享进程
        try:
            limits.check_pattern(bad)
            raise AssertionError(f"灾难性回溯正则竟通过: {bad}")
        except ValueError as e:
            assert "回溯" in str(e)
    assert limits.check_pattern(r"(?P<y>\d{4})-\d{2}")

    with tempfile.TemporaryDirectory() as td:
        root = _P(td) / "exports"
        root.mkdir()
        (root / "ok.csv").write_text("a\n1\n", encoding="utf-8")
        outside = _P(td) / "secret.txt"
        outside.write_text("top", encoding="utf-8")

        os.environ.pop("MCP_FILE_ROOTS", None)
        try:
            limits.safe_path(str(root / "ok.csv"))
            raise AssertionError("未配白名单时不应放行任意文件读取")
        except ValueError as e:
            assert "MCP_FILE_ROOTS" in str(e)

        os.environ["MCP_FILE_ROOTS"] = str(root)
        try:
            assert limits.safe_path(str(root / "ok.csv")).name == "ok.csv"
            for bad in (str(outside), str(root / ".." / "secret.txt")):
                try:
                    limits.safe_path(bad)
                    raise AssertionError(f"白名单外路径应被拒: {bad}")
                except ValueError as e:
                    assert "越界" in str(e)
        finally:
            os.environ.pop("MCP_FILE_ROOTS", None)
    print("PASS  shared·limits 护栏（规模上限 / ReDoS / 路径围栏）")


def test_shared_audit() -> None:
    """审计：敏感键遮蔽、长值截断、尾部查询、入参指纹与键序无关。"""
    import tempfile

    from mcp_shared import audit

    with tempfile.TemporaryDirectory() as td:
        os.environ["MCP_AUDIT_DIR"] = td
        audit._INSTANCES.clear()
        log = audit.audit_log("smoke")
        log.record("tool_call", tool="demo",
                   args={"dsn": "postgres://u:p@h/db", "token": "abc", "page": 1, "big": "y" * 5000})
        line = log.query(1)[0]
        assert line["args"]["dsn"] == "***" and line["args"]["token"] == "***", line
        assert line["args"]["page"] == 1
        assert "len=5000" in line["args"]["big"], "长文本应截断并标注原长"
        log.record("tool_error", tool="demo", error="boom")
        assert [e["event"] for e in log.query(5)] == ["tool_error", "tool_call"], "应最新在前"
        assert log.query(5, event="tool_error")[0]["error"] == "boom"
        assert audit.digest({"a": 1, "b": 2}) == audit.digest({"b": 2, "a": 1})
        assert audit.digest({"a": 1}) != audit.digest({"a": 2})
        # 轮转：长期运行不能把磁盘写满
        os.environ["MCP_AUDIT_MAX_BYTES"] = "4000"
        os.environ["MCP_AUDIT_KEEP"] = "2"
        for i in range(60):
            log.record("tool_call", tool="rot", args={"i": i, "pad": "z" * 120})
        names = sorted(os.listdir(td))
        assert "smoke.jsonl.1" in names and "smoke.jsonl.2" in names, names
        assert "smoke.jsonl.3" not in names, "超出 KEEP 份数的最旧文件应被丢弃"
        assert log.query(1)[0]["args"]["i"] >= 59, "轮转后仍能读到最新事件"
        os.environ.pop("MCP_AUDIT_MAX_BYTES", None)
        os.environ.pop("MCP_AUDIT_KEEP", None)
        audit._INSTANCES.clear()
        os.environ.pop("MCP_AUDIT_DIR", None)
    print("PASS  shared·audit 落盘（脱敏 / 截断 / 查询 / 轮转）")


def test_http_frontend_guards() -> None:
    """HTTP 前置层：Bearer 401、/healthz 免鉴权、滑动窗口限流 429。"""
    import asyncio

    from mcp_shared import http_kit

    calls = {"n": 0}

    async def inner(scope, receive, send):
        calls["n"] += 1
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})

    def scope(path="/mcp", headers=None, client=("10.0.0.1", 5555)):
        return {"type": "http", "method": "POST" if path == "/mcp" else "GET", "path": path,
                "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
                "client": client}

    # 三种写法都要认（企业平台常把裸令牌直接塞进自定义请求头）
    for variant in ("sekret", "Bearer sekret", "bearer  sekret", "  Bearer sekret  ", "Token sekret"):
        assert http_kit._credential_matches(variant, "sekret"), variant
    assert not http_kit._credential_matches("Basic sekret", "sekret")
    assert not http_kit._credential_matches("", "sekret")
    assert http_kit._credential_matches(None, "")  # 未启用鉴权

    front = http_kit.HttpFrontend(inner, server_name="t", token="sekret", rate_limit_per_second=2)

    async def run(sc):
        out = {}

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(msg):
            out.setdefault("status", msg.get("status"))
            out.setdefault("headers", msg.get("headers", []))

        await front(sc, receive, send)
        return out

    async def flow():
        r = await run(scope("/mcp"))
        assert r["status"] == 401 and calls["n"] == 0, "缺 token 必须 401 且不进内层"
        r = await run(scope("/mcp", {"authorization": "Bearer wrong"}))
        assert r["status"] == 401
        r = await run(scope("/healthz"))
        assert r["status"] == 200 and calls["n"] == 0, "/healthz 免鉴权且不转发"
        # 裸令牌也应放行（换个客户端，避免占掉下面限流用例的配额）
        r = await run(scope("/mcp", {"authorization": "sekret"}, client=("10.0.0.9", 1)))
        assert r["status"] == 200, "裸令牌（无 Bearer 前缀）被拒"
        for _ in range(2):
            r = await run(scope("/mcp", {"authorization": "Bearer sekret"}))
            assert r["status"] == 200
        r = await run(scope("/mcp", {"authorization": "Bearer sekret"}))
        assert r["status"] == 429, "超过 2/s 应限流"
        assert any(k == b"retry-after" for k, _ in r["headers"]), "429 需带 Retry-After"
        r = await run(scope("/mcp", {"authorization": "Bearer sekret"}, client=("10.0.0.2", 1)))
        assert r["status"] == 200, "限流应按客户端分桶"

    asyncio.run(flow())
    assert calls["n"] == 4, calls
    print("PASS  shared·http 前置层（Bearer / healthz / 限流分桶）")


def test_downstream_failure_class() -> None:
    """下游失败分类：不可达给可读建议，写工具绝不自动重试。"""
    from mcp_shared.mcp_client import (
        DownstreamError,
        call_downstream_http,
        retries_for,
        timeout_for,
    )

    assert retries_for("data_batch_import", read_only=False) == 0, "写工具绝不能自动重试（会重复落库）"
    assert retries_for("data_list_tables", read_only=True) >= 1
    os.environ["MCP_DOWNSTREAM_TIMEOUT_DATA_SLOW_TOOL"] = "3"
    try:
        assert timeout_for("data_slow_tool") == 3.0 and timeout_for("other") == 60.0
    finally:
        os.environ.pop("MCP_DOWNSTREAM_TIMEOUT_DATA_SLOW_TOOL")

    try:
        call_downstream_http("http://127.0.0.1:59999/mcp", "x", {}, read_only=False, timeout=2)
        raise AssertionError("不可达下游竟然成功")
    except DownstreamError as e:
        assert e.kind == "unreachable", e.kind
        assert "list_downstreams" in e.hint or "refresh_routes" in e.hint, e.hint
        assert e.attempts == 1, f"写工具不应重试: {e.attempts}"
    # SDK 会把 HTTP 状态码吞成 MCPError，靠响应钩子记下的真实码才能报对"鉴权失败"
    from mcp_shared.mcp_client import classify_http_status

    assert classify_http_status([]) is None
    kind, hint, retry = classify_http_status([401])
    assert kind == "unauthorized" and retry is False and "MCP_DOWNSTREAM_TOKEN" in hint, hint
    assert classify_http_status([503])[0] == "downstream_5xx"
    assert classify_http_status([404])[0] == "not_found"
    print("PASS  shared·下游失败分类（不可达/鉴权→可读建议，写调用不重试）")


def test_gateway_kv_and_schema() -> None:
    """入参形态归一：kv 引号保真、脏参数结构化报错、路由别名。"""
    import mcp_gateway.main as gw

    parsed = gw._parse_kv_params(
        "id=007;version=1.10;on=true;qty=3;ratio=1.5;note=null;msg='v2.0.0';t=a=1"
    )
    assert parsed == {"id": "007", "version": "1.10", "on": True, "qty": 3, "ratio": 1.5,
                      "note": None, "msg": "v2.0.0", "t": "a=1"}, parsed
    assert gw._parse_kv_params("a=1;b='x;y'") == {"a": 1, "b": "x;y"}, "引号内分号不算分隔符"
    assert gw._coerce_args('{"a": 1}') == {"a": 1} and gw._coerce_args(None) == {}
    for bad in ("not-a-kv", '{"a": 1', "[1,2]", 12):
        try:
            gw._coerce_args(bad)
            raise AssertionError(f"脏 arguments 应报错: {bad!r}")
        except Exception as e:
            assert "arguments" in str(e) or "params" in str(e), e
    assert gw._route_tool_name("go_datahub") == "route_go_datahub"
    assert gw._norm("Common-Tools") == "common_tools"
    print("PASS  网关·入参形态归一（kv 引号 / JSON / 脏参数可读报错）")


def test_itops_dataset_enum_from_config() -> None:
    """第 2 层枚举由配置驱动：DATASET_*_DOMAIN=itops 决定本域取值与中文说明。"""
    from mcp_itops import main as itops

    tool = itops.mcp._tool_manager.get_tool("itops_query_dataset")
    enum = tool.parameters["properties"]["dataset_id"]["enum"]
    assert "incidents" in enum and "assets" in enum, enum
    assert "orders" not in enum, "非本域数据集不得出现在领域枚举里"
    desc = tool.parameters["properties"]["dataset_id"].get("description", "")
    assert "incidents=" in desc, f"参数描述应带中文说明：{desc}"
    os.environ["DATASET_TICKETS_DOMAIN"] = "finance"
    try:
        try:
            itops._guard_domain("tickets")
            raise AssertionError("越域未拦截")
        except Exception as e:
            assert "data.query_dataset" in str(e) or "第 3 层" in str(e), e
    finally:
        os.environ.pop("DATASET_TICKETS_DOMAIN", None)
    print("PASS  it_ops·数据集枚举配置驱动（含越域引导）")


def test_external_host_allowed() -> None:
    """回归：绑定 0.0.0.0 对外部署时，外部 Host/Origin 不得被 DNS-rebinding 防护拦掉。

    踩过的坑：run_server 自己套前置层后漏传 host 给 streamable_http_app()，SDK 按默认
    127.0.0.1 开启防护 → AI 平台用 http://内网IP:9000 连就 421/403，界面显示成 500。
    """
    import httpx2
    import uvicorn

    from mcp_common_server import main as common
    from mcp_shared.http_kit import HttpFrontend

    app = HttpFrontend(common.mcp.streamable_http_app(host="0.0.0.0"), server_name="t")
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not srv.started:
        if time.time() > deadline:
            raise RuntimeError("HTTP server 启动超时")
        time.sleep(0.05)
    try:
        port = srv.servers[0].sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/mcp"
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "p", "version": "1"}}}
        base = {"content-type": "application/json",
                "accept": "application/json, text/event-stream"}
        cases = {
            "外部 IP Host": {**base, "host": f"10.45.34.223:{port}"},
            "外部 Origin": {**base, "origin": f"http://10.45.34.223:{port}"},
            "本机 Host": {**base, "host": f"127.0.0.1:{port}"},
        }
        for label, headers in cases.items():
            r = httpx2.post(url, headers=headers, json=body, timeout=10)
            assert r.status_code == 200, f"{label} 被拒：{r.status_code} {r.text[:80]}"
        # 反向确认：绑定本机时防护仍应生效（不是把安全一起关掉）
        app2 = HttpFrontend(common.mcp.streamable_http_app(host="127.0.0.1"), server_name="t2")
        srv2 = uvicorn.Server(uvicorn.Config(app2, host="127.0.0.1", port=0, log_level="error"))
        th2 = threading.Thread(target=srv2.run, daemon=True)
        th2.start()
        d2 = time.time() + 10
        while not srv2.started:
            if time.time() > d2:
                raise RuntimeError("HTTP server 启动超时")
            time.sleep(0.05)
        try:
            p2 = srv2.servers[0].sockets[0].getsockname()[1]
            r = httpx2.post(f"http://127.0.0.1:{p2}/mcp",
                            headers={**base, "host": f"10.45.34.223:{p2}"}, json=body, timeout=10)
            assert r.status_code == 421, f"本机绑定应拦外部 Host，实为 {r.status_code}"
        finally:
            srv2.should_exit = True
            th2.join(timeout=5)
    finally:
        srv.should_exit = True
        th.join(timeout=5)
    print("PASS  传输层·对外部署 Host/Origin 放行（本机绑定仍防护）")


def test_gateway_identity_channels() -> None:
    """审批令牌的两条通道与真实身份：请求头优先，AI 侧无令牌即无法自批。"""
    import mcp_gateway.main as gw
    from mcp_shared.http_kit import actor_hint, approval_hint

    os.environ["MCP_APPROVAL_TOKEN"] = "hdr-secret"
    try:
        # 1) 无令牌（AI 通道的典型情形）→ 必须拒
        try:
            gw._require_approve_authority("apr_x", None)
            raise AssertionError("无令牌竟然放行")
        except Exception as e:
            assert "审批令牌" in str(e), e

        # 2) 请求头带令牌（审批人通道）→ 放行，且不需要把令牌写进入参
        tok = approval_hint.set("hdr-secret")
        try:
            gw._require_approve_authority("apr_x", None)
        finally:
            approval_hint.reset(tok)

        # 3) 参数带令牌同样可用（运维脚本）
        gw._require_approve_authority("apr_x", "hdr-secret")

        # 4) 错令牌仍拒
        tok2 = approval_hint.set("wrong")
        try:
            try:
                gw._require_approve_authority("apr_x", None)
                raise AssertionError("错令牌竟然放行")
            except Exception as e:
                assert "审批令牌" in str(e), e
        finally:
            approval_hint.reset(tok2)

        # 5) 身份：X-Sg-Actor 优先于 AI 自报的 requested_by
        assert gw._actor("ai-agent") == "ai-agent"
        t3 = actor_hint.set("zhangsan@corp")
        try:
            assert gw._actor("ai-agent") == "zhangsan@corp"
            assert gw._identity_extras().get("principal") == "zhangsan@corp"
        finally:
            actor_hint.reset(t3)
    finally:
        os.environ.pop("MCP_APPROVAL_TOKEN", None)
    print("PASS  网关·审批令牌双通道与真实身份（请求头优先）")


if __name__ == "__main__":
    test_lower_common()
    test_middle_itops()
    test_gateway_aggregation_flow()
    test_gateway_kv_and_schema()
    test_itops_persistence()
    test_itops_degrades_without_sqlite()
    test_itops_dataset_enum_from_config()
    test_shared_config()
    test_config_inline_comments()
    test_shared_dsrouting()
    test_shared_limits()
    test_shared_audit()
    test_http_frontend_guards()
    test_downstream_failure_class()
    test_gateway_identity_channels()
    test_external_host_allowed()
    print("\n全部冒烟测试通过 ✅")
