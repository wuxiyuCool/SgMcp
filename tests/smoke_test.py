"""端到端冒烟测试（协议级）：用官方 SDK v2 的 Client 验证三层链路。

与旧版「直接调 Python 函数」不同，本测试让调用真实经过 MCP 协议层
（工具 schema 生成、JSON 序列化、结果解包），更接近客户端真实行为：

1. 下层 common：util_get_encoded（slug）/ util_get_time（含 Asia/Shanghai 时区）。
2. 中层 it_ops：ITOM 工具治理（清单/注解/错误码），ITOM 平台用进程内 FakeHttp
   仿真（契约测试不碰真实平台）。
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

# 审计/审批台账隔离到系统临时目录（避免冒烟污染仓库）
import tempfile  # noqa: E402

_TMP = Path(tempfile.gettempdir()) / "sgmcp_smoke"
_TMP.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_AUDIT_DIR", str(_TMP / "audit"))
os.environ.setdefault("MCP_APPROVAL_STORE", str(_TMP / "audit" / "approvals.jsonl"))


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

    # 工具数量治理：恰好 8 个 util_*（旧 13 个无前缀工具已合并下线）
    names = asyncio.run(_list_tools(common.mcp))
    assert names == {
        "util_get_time", "util_create_id", "util_get_encoded", "util_get_json",
        "util_get_url", "util_search_text", "util_calc_stats", "util_get_status",
    }, names

    d = _payload(asyncio.run(_call(common.mcp, "util_get_encoded",
                                   {"text": "Hello World!", "algo": "slug"})))
    assert d["result"] == "hello-world", d

    d = _payload(asyncio.run(_call(common.mcp, "util_get_time",
                                   {"timezone_name": "Asia/Shanghai"})))
    assert d["is_now"] is True and "+08:00" in d["converted"], d

    # 错误契约：非法入参带 [COMMON_<CODE>]，AI 可按码纠正
    r = asyncio.run(_call(common.mcp, "util_get_time", {"timezone_name": "北京"}))
    assert r.is_error and "[COMMON_INVALID_INPUT]" in r.content[0].text, r.content
    print("PASS  下层·通用工具（协议级，util.* 前缀 + 8 工具治理）")


def test_middle_itops() -> None:
    """协议级：ITOM 工具清单治理 + 无凭据可用的发现工具 + 错误码契约。

    开发机的 config/platform.env 可能配了真实账户，这里隔离全部 MCP_ITOM_* 环境，
    保证断言不受本机配置影响。
    """
    from mcp_itops import main as itops
    from mcp_shared import config as shared_config

    # 隔离本机 config/platform.env：pop 已注入的键 + 冻结懒加载（否则首次 get()
    # 会把文件值重新注入 os.environ，隔离失效）
    saved = {k: v for k, v in os.environ.items() if k.startswith("MCP_ITOM_")}
    for k in saved:
        os.environ.pop(k, None)
    lazy_was = shared_config._loaded
    shared_config._loaded = True
    try:
        # 1) 工具数量治理：恰好 8 个、命名稳定（本地台账/数仓/批处理演示工具已下线）
        names = asyncio.run(_list_tools(itops.mcp))
        assert names == {
            "itops_list_itom_accounts", "itops_get_itom_status", "itops_delete_itom_sessions",
            "itops_query_itom", "itops_submit_itom", "itops_query_itom_incidents",
            "itops_list_itom_options", "itops_create_itom_incident",
        }, names

        # 2) 注解读写分离：写工具 readOnlyHint=False，只读工具 True（网关重试策略依赖它）
        tm = itops.mcp._tool_manager
        write_tools = {"itops_submit_itom", "itops_create_itom_incident", "itops_delete_itom_sessions"}
        for name in sorted(names):
            ann = tm.get_tool(name).annotations
            assert ann is not None, f"{name} 缺 annotations"
            assert bool(ann.read_only_hint) is (name not in write_tools), name

        # 3) 无凭据也能用的发现工具：账户清单（空）与自检（只回形态不回凭据）
        rows = _payload(asyncio.run(_call(itops.mcp, "itops_list_itom_accounts", {})))
        if isinstance(rows, dict) and set(rows) == {"result"}:
            rows = rows["result"]
        assert rows == [], rows
        status = _payload(asyncio.run(_call(itops.mcp, "itops_get_itom_status", {})))
        assert status["accounts"] == [] and status["url_set"] is False, status
        assert status["hint"] and "MCP_ITOM_UID_PARAM" in status["hint"], status

        # 4) kind=form：建单表单说明书并入 options 工具（原独立表单工具已删）
        form = _payload(asyncio.run(_call(itops.mcp, "itops_list_itom_options",
                                          {"account_ref": "x", "kind": "form"})))
        if isinstance(form, dict) and set(form) == {"result"}:
            form = form["result"]
        fields = {f["field"] for f in form}
        assert {"dept", "group", "system", "subclass", "menu", "reporter",
                "handler", "description"} <= fields, fields

        # 5) 错误码契约：配置缺失给 [ITOM_NOT_CONFIGURED] 前缀 + 建议，而不是裸栈
        r = asyncio.run(_call(itops.mcp, "itops_query_itom",
                              {"account_ref": "wangxu", "path": "/event-manage/findByPage"}))
        assert r.is_error and "[ITOM_NOT_CONFIGURED]" in " ".join(c.text for c in r.content), r.content
    finally:
        os.environ.update(saved)
        shared_config._loaded = lazy_was
    print("PASS  中层·it_ops（8 工具治理 + 注解读写分离 + 错误码契约）")


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


class _FakeResp:
    """ITOM 仿真的最小 HTTP 响应（与 itom 客户端用到的属性对齐）。"""

    def __init__(self, status=200, payload=None, text="", cookies=None):
        self.status_code, self._payload, self.text = status, payload, text
        self.cookies = cookies or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeItomHttp:
    """进程内仿真 ITOM 平台：auto 模式下任意请求都回成功信封，可指定失败路径。

    it_ops 下游与网关同进程，替换 itom.httpx2 即可让审批流「批准后真实执行」
    走通，而不用连真实平台。
    """

    HTTPError = type("FakeHTTPError", (BaseException,), {})

    def __init__(self):
        self.calls: list[dict] = []
        self.fail_paths: tuple[str, ...] = ()

    def request(self, method, url, *, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params,
                           "json": json, "headers": dict(headers or {})})
        if any(p in url for p in self.fail_paths):
            return _FakeResp(status=500, text="boom", payload={"error": "boom"})
        return _FakeResp(payload={"retCode": "0000000", "retDesc": "OK",
                                  "rspBody": {"ok": 1, "id": "E1"}})


def test_gateway_aggregation_flow() -> None:
    import mcp_common_server.main as common
    import mcp_itops.main as itops
    from mcp_itops import itom as itom_mod

    # ITOM 仿真环境：静态令牌账户（不发登录请求）+ 任意请求回成功信封
    itom_env_saved = {k: os.environ.get(k) for k in (
        "MCP_ITOM_URL", "MCP_ITOM_ACCOUNTS", "MCP_ITOM_WANGXU_TOKEN",
        "MCP_ITOM_UID_PARAM", "MCP_ITOM_RANDOM_QUERY", "MCP_ITOM_READ_SEGMENTS",
        "MCP_ITOM_TOKEN_HEADER")}
    os.environ.update({
        "MCP_ITOM_URL": "https://itom.test/api",
        "MCP_ITOM_ACCOUNTS": "wangxu",
        "MCP_ITOM_WANGXU_TOKEN": "static-tok",
        "MCP_ITOM_UID_PARAM": "",
        "MCP_ITOM_RANDOM_QUERY": "",
        "MCP_ITOM_READ_SEGMENTS": "",
        "MCP_ITOM_TOKEN_HEADER": "Authorization",
    })
    real_http = itom_mod.httpx2
    fake = _FakeItomHttp()
    itom_mod.httpx2 = fake
    itom_mod._sessions.clear()
    itom_mod._last_source.clear()

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
                    {"server": "common-tools", "tool": "util_get_encoded",
                     "arguments": {"text": "Hello Gateway", "algo": "slug"}},
                )
                d = _payload(r)
                assert d["approved"] is True and d["executed"] is True \
                    and d["result"]["result"] == "hello-gateway", d
                print("PASS  网关·只读工具直接放行（HTTP 转发）")

                # 4b) arguments 以 JSON 字符串传输（部分 AI 平台会序列化嵌套对象）→ 容错生效
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "common-tools", "tool": "util_get_encoded",
                     "arguments": '{"text": "Str Args", "algo": "slug"}'},
                )
                d = _payload(r)
                assert d["executed"] is True and d["result"]["result"] == "str-args", d
                print("PASS  网关·arguments 兼容 JSON 字符串形式")

                # 4c) 路由宽容解析：脏 server（大小写/连字符）与漏层级前缀的 tool 都能救回
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "Common-Tools", "tool": "util_get_encoded",
                     "arguments": {"text": "Sloppy Input", "algo": "slug"}},
                )
                assert _payload(r)["result"]["result"] == "sloppy-input"
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "query_itom",
                     "arguments": {"account_ref": "wangxu", "path": "/event-manage/findByPage",
                                   "method": "POST"}},
                )
                d = _payload(r)
                assert d["executed"] is True and d["result"]["ok"] is True, d
                print("PASS  网关·server/tool 宽容解析（归一化+补层级前缀）")

                # 4d) 入口精简：gateway_call 直接吃扁平 kv 串（原 gateway_call_kv 已并入）
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "common-tools", "tool": "util_get_encoded",
                     "arguments": "text=kv-args;algo=slug"},
                )
                d = _payload(r)
                assert d["executed"] is True and d["result"]["result"] == "kv-args", d
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "query_itom",
                     "arguments": "account_ref=wangxu;path=/event-manage/findByPage;method=POST"},
                )
                d = _payload(r)
                assert d["executed"] is True and d["result"]["ok"] is True, d
                # 下划线别名（规避平台对参数值中 "." 的序列化缺陷）：itops_query_itom
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops_query_itom",
                     "arguments": {"account_ref": "wangxu", "path": "/event-manage/findByPage"}},
                )
                d = _payload(r)
                assert d["executed"] is True and d["result"]["ok"] is True, d
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
                assert "itops_submit_itom" in gw_tools["route_it_ops"].description, "描述须列出审批方法"
                # 经 route 工具直接调用（kv 扁平串入参）
                r = await client.call_tool(
                    "route_common_tools",
                    {"method": "util_get_encoded", "params": "text=route-fn;algo=slug"}
                )
                d = _payload(r)
                assert d["executed"] is True and d["result"]["result"] == "route-fn", d
                # 经 route 工具触发审批闸门
                r = await client.call_tool(
                    "route_it_ops", {"method": "itops_submit_itom",
                                     "params": {"account_ref": "wangxu",
                                                "path": "/event-manage/updateEvent"}}
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
                slug_entry = next(t for t in gmap["common-tools"]["tools"]
                                  if t["tool"] == "util_get_encoded")
                assert slug_entry["params"]["text"]["required"] is True
                assert slug_entry["params"]["algo"]["enum"] == \
                    ["md5", "sha1", "sha256", "sha512", "base64", "slug"]
                print("PASS  网关·list_tool_catalog（MCP 归属 + 调用链 + 参数说明书）")

                # 5) 非审批写工具（会话清理）经 HTTP 转发执行到 it_ops
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops_delete_itom_sessions",
                     "arguments": {"account_ref": "wangxu"}},
                )
                d = _payload(r)
                assert d["executed"] is True and d["result"]["ok"] is True, d
                print("PASS  网关·非审批写工具经 HTTP 转发执行")

                # 6) 高风险写操作：挂起，创建审批单，不执行
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops_submit_itom",
                     "arguments": {"account_ref": "wangxu",
                                   "path": "/event-manage/updateEvent"}},
                )
                d = _payload(r)
                assert d["approved"] is False and d["executed"] is False
                rid = d["request_id"]
                print(f"PASS  网关·高风险写操作挂起（request_id={rid}）")

                # 7) 批准后才真正执行（HTTP 转发到 it_ops → ITOM 仿真回成功）
                r = await client.call_tool("approve_request", {"request_id": rid})
                d = _payload(r)
                assert d["executed"] is True and d["result"]["ok"] is True, d
                print("PASS  网关·批准后经 HTTP 转发执行")

                # 8) 驳回：不执行
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops_submit_itom",
                     "arguments": {"account_ref": "wangxu",
                                   "path": "/event-manage/updateEvent"}},
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
                    {"server": "it_ops", "tool": "query_itomm", "arguments": {}},
                )
                assert r.is_error and "itops_query_itom" in str(r.content), \
                    f"未命中时应给出相近工具名: {r.content}"
                print("PASS  网关·未命中路由给相近工具名")

                # 13) 审批令牌：配了 MCP_APPROVAL_TOKEN 后，AI 侧无令牌无法自批
                os.environ["MCP_APPROVAL_TOKEN"] = "human-only-secret"
                try:
                    r = await client.call_tool(
                        "gateway_call",
                        {"server": "it_ops", "tool": "itops_submit_itom",
                         "arguments": {"account_ref": "wangxu",
                                       "path": "/event-manage/updateEvent"}},
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
                fake.fail_paths = ("/boom",)
                try:
                    r = await client.call_tool(
                        "gateway_call",
                        {"server": "it_ops", "tool": "itops_submit_itom",
                         "arguments": {"account_ref": "wangxu",
                                       "path": "/boom/updateEvent"}},
                    )
                    rid4 = _payload(r)["request_id"]
                    r = await client.call_tool("approve_request", {"request_id": rid4})
                    assert r.is_error, "下游必然失败的场景竟然执行成功"
                    r = await client.call_tool("list_approvals", {"status": "exec_failed"})
                    failed_ids = [x["id"] for x in _items(_payload(r))]
                    assert rid4 in failed_ids, f"执行失败的审批单应可查（exec_failed）: {failed_ids}"
                    assert "attempts" in next(x for x in _items(_payload(r)) if x["id"] == rid4)
                finally:
                    fake.fail_paths = ()
                print("PASS  网关·批准但下游失败 → exec_failed 可追溯/可重放")

                # 15) 幂等键：同一 key 的重复写请求不会二次执行（AI 重试安全）
                # 用非审批写工具 delete_itom_sessions：submit_itom 会先挂审批单
                # （返回 request_id 而非执行结果），不适合直接验证结果级幂等
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops_delete_itom_sessions",
                     "arguments": {"account_ref": "wangxu"},
                     "idempotency_key": "smoke-key-1"},
                )
                first = _payload(r)
                assert first.get("executed") is True, first
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "it_ops", "tool": "itops_delete_itom_sessions",
                     "arguments": {"account_ref": "wangxu"},
                     "idempotency_key": "smoke-key-1"},
                )
                again = _payload(r)
                assert again["result"] == first["result"] \
                    and again.get("idempotent_replay") is True, again
                print("PASS  网关·idempotency_key 幂等重放（重试不重复执行）")

                # 16) 结果规模上限：超限截断并引导改用过滤/异步（护住上下文与 token）
                gw.MAX_RESULT_ITEMS = 3
                try:
                    r = await client.call_tool(
                        "gateway_call",
                        {"server": "it_ops", "tool": "itops_list_itom_options",
                         "arguments": {"account_ref": "wangxu", "kind": "form"}},
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
                    {"server": "common-tools", "tool": "util_calc_stats",
                     "arguments": "text='编号 007 与版本 1.10'"},
                )
                d = _payload(r)
                assert d["executed"] is True and d["result"]["chars"] == 15 \
                    and d["result"]["chars_no_space"] == 12, \
                    f"007/1.10 应原样透传不被误转数值: {d}"
                print("PASS  网关·kv 引号保原样（007/1.10 不被误转数值）")

                # 18) 审计落盘：调用与审批可回查，敏感入参已脱敏
                r = await client.call_tool(
                    "gateway_call",
                    {"server": "common-tools", "tool": "util_get_encoded",
                     "arguments": {"text": "hi", "algo": "slug",
                                   "token": "super-secret-value"}},
                )
                _payload(r)
                r = await client.call_tool("query_audit_log", {"limit": 50})
                events = _items(_payload(r))
                assert any(e["event"] == "tool_call" for e in events), events[:2]
                assert any(e["event"] in {"approval_created", "approval_decided"} for e in events)
                leaked = [e for e in events if "super-secret-value" in json.dumps(e, ensure_ascii=False)]
                assert not leaked, f"审计日志泄露敏感入参: {leaked}"
                masked = next(e for e in events
                              if e.get("tool") == "util_get_encoded" and "args" in e)
                assert masked["args"]["token"] == "***", masked
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
        # 环境与仿真 HTTP 必须还原：泄漏的 MCP_ITOM_WANGXU_TOKEN 会让后续测试里
        # 的 wangxu 变成静态令牌账户（_session 直接返回、不再走真实登录），
        # 进而破坏 test_itom_account_ref_client 的登录断言
        for k, v in itom_env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        itom_mod.httpx2 = real_http
        itom_mod._sessions.clear()
        itom_mod._last_source.clear()


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


def test_itom_account_ref_client() -> None:
    """ITOM 客户端：口令只在服务端配置里流转，AI 侧只见 account_ref；
    登录/token 注入/401 自动重登/站内路径围栏都不许外泄凭据。"""
    import json as _json

    import httpx2 as _httpx

    from mcp_itops import itom

    class Resp:
        def __init__(self, status=200, payload=None, text="", cookies=None):
            self.status_code, self._payload, self.text = status, payload, text
            self.cookies = cookies or {}

        def json(self):
            if self._payload is None:
                raise ValueError("no json")
            return self._payload

    class FakeHttp:
        HTTPError = _httpx.HTTPError

        def __init__(self):
            self.calls: list[dict] = []
            self.script: list[Resp] = []

        def request(self, method, url, *, params=None, json=None, headers=None, timeout=None):
            self.calls.append({"method": method, "url": url, "params": params,
                               "json": json, "headers": dict(headers or {})})
            assert self.script, f"没有预置响应，请求={method} {url}"
            return self.script.pop(0)

    saved = {k: os.environ.get(k) for k in (
        "MCP_ITOM_URL", "MCP_ITOM_ACCOUNTS", "MCP_ITOM_WANGXU_USER_ID",
        "MCP_ITOM_WANGXU_PASSWORD", "MCP_ITOM_WANGXU_ORG_SID",
        "MCP_ITOM_WANGXU_ORG_POSITION_SID", "MCP_ITOM_WANGXU_NOTE",
        "MCP_ITOM_OPS01_TOKEN", "MCP_ITOM_UID_PARAM", "MCP_ITOM_RANDOM_QUERY",
        "MCP_ITOM_READ_SEGMENTS", "MCP_ITOM_TOKEN_HEADER")}
    real_http = itom.httpx2
    try:
        os.environ.update({
            "MCP_ITOM_URL": "https://itom.test/api",
            "MCP_ITOM_UID_PARAM": "",          # 本例测的是「token 进请求头」的默认形态
            "MCP_ITOM_RANDOM_QUERY": "",
            "MCP_ITOM_READ_SEGMENTS": "",
            "MCP_ITOM_TOKEN_HEADER": "Authorization",
            "MCP_ITOM_ACCOUNTS": "wangxu,ops01",
            "MCP_ITOM_WANGXU_USER_ID": "wangxu",
            "MCP_ITOM_WANGXU_PASSWORD": "s3cr3t|中文",
            "MCP_ITOM_WANGXU_ORG_SID": "266",
            "MCP_ITOM_WANGXU_ORG_POSITION_SID": "1154",
            "MCP_ITOM_WANGXU_NOTE": "运维中心",
            "MCP_ITOM_OPS01_TOKEN": "static-tok",
        })
        itom._sessions.clear()
        itom._last_source.clear()
        fake = FakeHttp()
        itom.httpx2 = fake

        # 1) 账户清单：有引用名与备注，没有口令；在册但没配全的要显式可见
        os.environ["MCP_ITOM_ACCOUNTS"] = "wangxu,ops01,ghost"
        rows = itom.accounts_summary()
        assert [r["account_ref"] for r in rows] == ["wangxu", "ops01", "ghost"], rows
        assert rows[0]["user_id"] == "w***u", rows[0]
        assert rows[2].get("configured") is False and "USER_ID" in rows[2]["reason"], rows[2]
        assert "s3cr3t" not in _json.dumps(rows, ensure_ascii=False), rows

        # 2) 未知引用名 → 引导性错误（列出可用），不是 KeyError；
        #    "default"/空 是模型的习惯写法，单账户清单下落到第一个引用名
        assert itom.account("default").ref == "wangxu", "default 应落到清单首个账户"
        assert itom.account("").ref == "wangxu"
        try:
            itom.account("zhangsan")
        except itom.ItomError as e:
            assert "wangxu, ops01" in str(e), e
        else:
            raise AssertionError("未知引用名应当报错")

        # 3) 登录：请求体带口令（服务端→平台，正常），返回状态里绝不带 token
        fake.script = [Resp(payload={"reqHeader": {}, "reqBody": {"token": "tk-123"}})]
        sess = itom._session(itom.account("wangxu"))
        st = {"token_source": itom._last_source.get("wangxu", "cookie"),
              "has_token": bool(sess.token), "cookie_names": sorted(sess.cookies)}
        assert st["token_source"] == "token" and st["has_token"], st
        assert "tk-123" not in _json.dumps(st, ensure_ascii=False), st
        call = fake.calls[-1]
        assert call["url"] == "https://itom.test/api/auth/login/login?uid=null", call
        assert call["json"]["reqBody"]["passwd"] == "s3cr3t|中文", call["json"]
        assert call["json"]["reqBody"]["orgSid"] == "266"

        # 4) 后续请求自动带 Authorization；token 不回显在返回值里
        fake.script = [Resp(payload={"data": [{"id": 1}]})]
        out = itom.request_json("wangxu", "GET", "/incident/list", params={"page": 1})
        assert fake.calls[-1]["headers"]["Authorization"] == "tk-123", fake.calls[-1]["headers"]
        assert fake.calls[-1]["params"] == {"page": 1}
        assert out["data"]["rows"] == [{"id": 1}], out["data"]
        assert "tk-123" not in _json.dumps(out, ensure_ascii=False)

        # 5) 平台侧提前失效：401 → 自动重登一次 → 重试成功
        fake.script = [Resp(status=401, payload={"message": "session expired"}),
                       Resp(payload={"reqBody": {"token": "tk-456"}}),
                       Resp(payload={"ok": 1})]
        itom._sessions["wangxu"].obtained_at = time.time()
        out = itom.request_json("wangxu", "GET", "/incident/list")
        assert out["data"] == {"ok": 1}, out
        assert [c["url"].split("/api")[-1] for c in fake.calls[-3:]] == [
            "/incident/list", "/auth/login/login?uid=null", "/incident/list"], fake.calls[-3:]
        assert fake.calls[-1]["headers"]["Authorization"] == "tk-456"

        # 6) 路径围栏：绝对 URL / .. / 查询串 / 协议相对 一律拒（挡 SSRF 与越界）
        for bad in ("http://evil.test/x", "/a/../../etc", "/a?b=1", "//evil.test", "incident/list"):
            try:
                itom.request_json("wangxu", "GET", bad)
            except itom.ItomError:
                continue
            raise AssertionError(f"path={bad} 应当被拒")

        # 7) static token 账户：不触发登录请求，直接带令牌
        fake.calls.clear()
        acct = itom.account("ops01")
        sess = itom._session(acct)
        assert acct.static_token == "static-tok" and sess.token == "static-tok", sess
        assert not fake.calls, "配了 TOKEN 的账户不该发登录请求"
        fake.script = [Resp(payload={"x": 1})]
        itom.request_json("ops01", "GET", "/todo/list")
        assert fake.calls[-1]["headers"]["Authorization"] == "static-tok", fake.calls[-1]["headers"]

        # 8) 纯 cookie 会话（Java 平台不给 token 的写法）也能登录
        itom._sessions.clear()
        os.environ["MCP_ITOM_WANGXU_PASSWORD"] = "s3cr3t|中文"
        fake.script = [Resp(payload={"reqBody": {"success": "true"}}, cookies={"JSESSIONID": "abc"})]
        sess = itom._session(itom.account("wangxu"))
        assert itom._last_source.get("wangxu") == "cookie" and not sess.token, sess
        assert sorted(sess.cookies) == ["JSESSIONID"], sess
        fake.script = [Resp(payload={"x": 1})]
        itom.request_json("wangxu", "GET", "/todo/list")
        assert fake.calls[-1]["headers"]["cookie"] == "JSESSIONID=abc", fake.calls[-1]["headers"]
    finally:
        itom.httpx2 = real_http
        itom._sessions.clear()
        itom._last_source.clear()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("PASS  it_ops·ITOM 预设账户客户端（凭据不外泄 / token 注入 / 401 重登 / 路径围栏）")


def test_itom_platform_contract() -> None:
    """按 ITOM 实测形态锁定契约：reqHeader/reqBody 请求信封、rspBody 响应信封、
    HTTP 200 + retCode≠0 要判失败、uid 走查询参数、只读 POST 判定与字段投影（PII 不外泄）。"""
    import json as _json

    from mcp_itops import itom

    class Resp:
        def __init__(self, status=200, payload=None, text="", cookies=None):
            self.status_code, self._payload, self.text = status, payload, text
            self.cookies = cookies or {}

        def json(self):
            if self._payload is None:
                raise ValueError("no json")
            return self._payload

    class FakeHttp:
        # 只当 httpx2.HTTPError 的类型占位：不能是 Exception 本身，否则测试里的
        # AssertionError 会被 itom 的错误分支吞掉，把"没预置响应"伪装成兜底成功
        HTTPError = type("FakeHTTPError", (BaseException,), {})

        def __init__(self):
            self.calls: list[dict] = []
            self.script: list[Resp] = []

        def request(self, method, url, *, params=None, json=None, headers=None, timeout=None):
            self.calls.append({"method": method, "url": url, "params": params,
                               "json": json, "headers": dict(headers or {})})
            assert self.script, f"没有预置响应，请求={method} {url}"
            return self.script.pop(0)

    saved = {k: os.environ.get(k) for k in (
        "MCP_ITOM_URL", "MCP_ITOM_ACCOUNTS", "MCP_ITOM_ITOMA_USER_ID",
        "MCP_ITOM_ITOMA_PASSWORD", "MCP_ITOM_UID_PARAM", "MCP_ITOM_READ_SEGMENTS",
        "MCP_ITOM_RANDOM_QUERY", "MCP_ITOM_TOKEN_HEADER")}
    real_http = itom.httpx2
    fake = FakeHttp()
    try:
        os.environ.update({
            "MCP_ITOM_URL": "https://itom.test/api",
            "MCP_ITOM_ACCOUNTS": "itoma",
            "MCP_ITOM_ITOMA_USER_ID": "itoma",
            "MCP_ITOM_ITOMA_PASSWORD": "pw",
            "MCP_ITOM_UID_PARAM": "uid",
        })
        itom._sessions.clear()
        itom._last_source.clear()
        itom.httpx2 = fake

        # 0) 自检只回形态不回值：uid 注入、账户清单、缺开关时的 hint
        sc = itom.selfcheck()
        assert sc["uid_param"] == "uid" and sc["hint"] is None, sc
        assert sc["accounts"] == ["itoma"] and sc["token_header"] is None, sc
        assert "pw" not in _json.dumps(sc, ensure_ascii=False), "自检不得回凭据"

        # 1) 登录：token 键是平台自定义的 gm_auth_token，会话靠 JSESSIONID
        fake.script = [Resp(payload={"retCode": "0000000", "retDesc": "OK", "timestamp": "t",
                                    "rspBody": {"gm_auth_token": "gm-1", "userId": "itoma"}},
                            cookies={"JSESSIONID": "js-1"})]
        sess = itom._session(itom.account("itoma"))
        st = {"token_source": itom._last_source.get("itoma", "cookie"),
              "has_token": bool(sess.token), "cookie_names": sorted(sess.cookies)}
        assert st["token_source"] == "gm_auth_token" and st["has_token"], st
        assert st["cookie_names"] == ["JSESSIONID"], st
        assert "gm-1" not in _json.dumps(st, ensure_ascii=False)

        # 2) 业务请求：服务端补 reqHeader/reqBody 信封，uid 走查询参数，不多发 Authorization
        rows = [{"eventNo": "SG1", "problemDesc": "入库单提取不了", "dealStaffPhone": "15000000000"},
                {"eventNo": "SG2", "problemDesc": "网络故障", "dealStaffPhone": "13900000000"}]
        fake.script = [Resp(payload={"retCode": "0000000", "retDesc": "OK",
                                    "rspBody": {"pageNum": 1, "total": 2, "resultData": rows}})]
        out = itom.request_json("itoma", "POST", "/event-manage/findByPage",
                                body={"pageSize": "20"}, fields=["eventNo", "problemDesc"])
        call = fake.calls[-1]
        assert call["json"] == {"reqHeader": {"operTitle": ""}, "reqBody": {"pageSize": "20"}}, call["json"]
        assert call["params"]["uid"] == "gm-1", call["params"]
        assert "Authorization" not in call["headers"], call["headers"]
        assert call["headers"]["cookie"] == "JSESSIONID=js-1"
        d = out["data"]
        assert d["rows"] == [{"eventNo": "SG1", "problemDesc": "入库单提取不了"},
                             {"eventNo": "SG2", "problemDesc": "网络故障"}], d
        assert (d["total"], d["rows_key"], d["returned"]) == (2, "resultData", 2), d
        # 手机号等 PII 不在投影里，就不该出现在返回中
        assert "15000000000" not in _json.dumps(out, ensure_ascii=False)

        # 3) HTTP 200 + retCode 非成功码 = 失败；但不是掉线的文案就不该乱重登
        logins = sum(1 for c in fake.calls if "/auth/login" in c["url"])
        fake.script = [Resp(payload={"retCode": "9999999", "retDesc": "参数不合法", "rspBody": None})]
        try:
            itom.request_json("itoma", "POST", "/event-manage/findByPage", body={})
        except itom.ItomError as e:
            assert "retCode=9999999" in str(e) and "参数不合法" in str(e), e
        else:
            raise AssertionError("retCode 失败必须报错")
        assert sum(1 for c in fake.calls if "/auth/login" in c["url"]) == logins, "业务错误不该触发重登"

        # 4) 会话失效（HTTP 200 + retCode 的掉线文案）→ 自动重登一次再重试，调用方无需先调 login
        assert itom._auth_expired({"retDesc": "当前用户登录信息发生变化请重新登录"}, {}), \
            "平台真实掉线文案必须命中重登词表"
        fake.script = [Resp(payload={"retCode": "9999999", "retDesc": "会话已失效，请重新登录", "rspBody": None}),
                       Resp(payload={"retCode": "0000000", "rspBody": {"gm_auth_token": "gm-2"}},
                            cookies={"JSESSIONID": "js-2"}),
                       Resp(payload={"retCode": "0000000", "rspBody": {"resultData": [{"eventNo": "SG9"}]}})]
        out = itom.request_json("itoma", "POST", "/event-manage/findByPage",
                                body={}, fields=["eventNo"])
        assert out["ok"] and out["relogged"] is True and out["data"]["rows"] == [{"eventNo": "SG9"}], out
        assert fake.calls[-1]["params"]["uid"] == "gm-2", fake.calls[-1]["params"]
        assert fake.calls[-1]["headers"]["cookie"] == "JSESSIONID=js-2"

        # 5) TTL 内复用会话：连续调用不重复登录
        logins = sum(1 for c in fake.calls if "/auth/login" in c["url"])
        for _ in range(3):
            fake.script = [Resp(payload={"retCode": "0000000", "rspBody": {}})]
            itom.request_json("itoma", "POST", "/event-manage/findByPage", body={})
        assert sum(1 for c in fake.calls if "/auth/login" in c["url"]) == logins, "TTL 内不该重复登录"

        # 6) 只读通道只放行 GET 或只读形态路径；写形态必须走审批工具
        fake.script = [Resp(payload={"retCode": "0000000", "rspBody": {"ok": 1}})]
        try:
            itom.request_json("itoma", "POST", "/event-manage/updateEvent", body={},
                              read_channel=True)
        except itom.ItomError as e:
            assert "不像只读接口" in str(e) and "itops_submit_itom" in str(e), e
        else:
            raise AssertionError("写形态路径不该走只读通道")
        assert itom.read_like("/event-manage/findByPage") and not itom.read_like("/event-manage/updateEvent")
        os.environ["MCP_ITOM_READ_SEGMENTS"] = "updateEvent"
        assert itom.read_like("/event-manage/updateEvent"), "追加白名单应生效"

        # 7) 已是完整信封时不重复包裹；cache buster 开关按需
        fake.script = [Resp(payload={"retCode": "0000000", "rspBody": {}})]
        itom.request_json("itoma", "POST", "/event-manage/findByPage",
                          body={"reqHeader": {"operTitle": "x"}, "reqBody": {"a": 1}})
        assert fake.calls[-1]["json"] == {"reqHeader": {"operTitle": "x"}, "reqBody": {"a": 1}}
        os.environ["MCP_ITOM_RANDOM_QUERY"] = "1"
        fake.script = [Resp(payload={"retCode": "0000000", "rspBody": {}})]
        itom.request_json("itoma", "POST", "/event-manage/findByPage", body={})
        qp = fake.calls[-1]["params"]
        assert any(k.startswith("0.") for k in qp) and qp["uid"] == "gm-2", qp

        # 8) 码值字典：内置快照兜底 → 平台字典覆盖 → 行内补中文（未知码值不编造）
        itom._dict_cache.clear()
        assert itom.code_table("EVENT_LEVEL") == {"1": "低", "2": "中", "3": "高", "4": "紧急"}
        fake.script = [
            Resp(payload={"retCode": "0000000",
                          "rspBody": {"resultData": [{"typeSid": 20052, "typeCode": "EVENT_LEVEL"}]}}),
            Resp(payload={"retCode": "0000000",
                          "rspBody": {"resultData": [{"typeCode": "1", "typeName": "低"},
                                                     {"typeCode": "5", "typeName": "特急"}]}}),
        ]
        assert itom.code_table("EVENT_LEVEL", "itoma") == {"1": "低", "5": "特急"}, "平台字典应覆盖快照"
        calls_before = len(fake.calls)
        assert itom.code_table("EVENT_LEVEL", "itoma") == {"1": "低", "5": "特急"}
        assert len(fake.calls) == calls_before, "码表结果要缓存，别每次都查两趟接口"
        rows = itom.decode_rows([{"eventState": "3", "eventLevel": "9", "eventNature": None}])
        assert rows[0]["eventStateName"] == "已解决", rows[0]
        assert "eventLevelName" not in rows[0] and "eventNatureName" not in rows[0], \
            "未知/空码值不该编造中文"
        # 事件单列表默认把状态/等级/性质翻译成中文（复用刚缓存的码表，不再打接口）
        fresh = time.time()
        itom._dict_cache.update({
            "EVENT_STATUS": ({"3": "已解决"}, fresh),
            "EVENT_LEVEL": ({"1": "低", "4": "紧急"}, fresh),
            "EVENT_NATURE": ({"2": "服务请求"}, fresh),
        })
        fake.script = [Resp(payload={"retCode": "0000000", "rspBody": {"total": 1, "resultData": [
            {"eventNo": "SG1", "eventState": "3", "eventLevel": "4", "eventNature": "2"}]}})]
        calls_before = len(fake.calls)
        listed = itom.incidents("itoma", days=7, page_size=1)["data"]["rows"]
        assert (listed[0]["eventStateName"], listed[0]["eventLevelName"],
                listed[0]["eventNatureName"]) == ("已解决", "紧急", "服务请求"), listed
        assert len(fake.calls) == calls_before + 1, "命中码表缓存时不该再查字典接口"
    finally:
        itom.httpx2 = real_http
        itom._sessions.clear()
        itom._last_source.clear()
        itom._dict_cache.clear()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("PASS  it_ops·ITOM 平台契约（信封/retCode/uid 参数/只读判定/字段投影）")



def test_itom_guided_incident_build() -> None:
    """引导式建单：候选发现带父级+角色、重名必须回问、payload 与平台前端形态一致，
    且档案接口返回的身份证号/token 必须被剔除。"""
    import json as _json

    from mcp_itops import itom

    class Resp:
        def __init__(self, payload=None, status=200):
            self.status_code, self._payload, self.text, self.cookies = status, payload, "", {}

        def json(self):
            return self._payload

    class FakeHttp:
        HTTPError = type("FakeHTTPError", (BaseException,), {})

        def __init__(self):
            self.calls: list[dict] = []
            self.script: list[Resp] = []

        def request(self, method, url, *, params=None, json=None, headers=None, timeout=None):
            self.calls.append({"url": url, "json": json, "params": params,
                               "headers": dict(headers or {})})
            assert self.script, f"没有预置响应，请求={method} {url}"
            return self.script.pop(0)

    def rows(*items) -> Resp:
        return Resp(payload={"retCode": "0000000", "rspBody": {"resultData": list(items)}})

    saved = {k: os.environ.get(k) for k in (
        "MCP_ITOM_URL", "MCP_ITOM_ACCOUNTS", "MCP_ITOM_GUIDED_TOKEN", "MCP_ITOM_UID_PARAM")}
    real_http = itom.httpx2
    fake = FakeHttp()
    try:
        os.environ.update({
            "MCP_ITOM_URL": "https://itom.test/api", "MCP_ITOM_ACCOUNTS": "guided",
            "MCP_ITOM_GUIDED_TOKEN": "tk", "MCP_ITOM_UID_PARAM": "uid",
        })
        itom.httpx2 = fake
        itom._sessions.clear()
        # 码表预置，避免测解析逻辑时还要脚本网关字典请求
        fresh = time.time()
        itom._dict_cache.update({
            "EVENT_LEVEL": ({"1": "低", "2": "中", "3": "高", "4": "紧急"}, fresh),
            "EVENT_NATURE": ({"1": "故障", "2": "服务请求"}, fresh),
            "SYS_USER_TYPE": ({"1": "一线人员", "2": "二线人员"}, fresh),
        })

        # 1) staff 必须带 role，否则平台返回 0 人
        try:
            itom.options("guided", "staff", parent=33298)
        except itom.ItomError as e:
            assert "role" in str(e), e
        else:
            raise AssertionError("staff 缺 role 应报错")

        # 2) 档案接口的敏感列必须被剔掉（平台 auth-user 类接口会带 idNo/token）
        fake.script = [Resp(payload={"retCode": "0000000", "rspBody": {"resultData": [
            {"userId": "wuxy1613", "userName": "吴希禹", "mobilePhone": "18700000000",
             "sysUserType": 1, "isDefault": 1, "idNo": "520201199604201613",
             "token": "d0e74945-secret"}]}})]
        staff = itom.options("guided", "staff", parent=33298, role="1")
        assert fake.calls[-1]["json"]["reqBody"] == {
            "pageSize": "50", "sysSid": 33298, "sysUserType": "1"}, fake.calls[-1]["json"]
        assert staff[0]["userName"] == "吴希禹"
        blob = _json.dumps(staff, ensure_ascii=False)
        assert "520201199604201613" not in blob and "d0e74945" not in blob, "敏感列泄漏"

        # 3) 重名必须回问，hint 能唯一命中才继续
        dup = [Resp(payload={"retCode": "0000000", "rspBody": {"resultData": [
            {"sid": 1, "customName": "鲁红文", "mobilePhone": "1771", "departentName": "焦化事业部"},
            {"sid": 2, "customName": "鲁红文", "mobilePhone": "1772", "departentName": "质检监督部",
             "operationName": "原燃料检测中心"}]}})]
        fake.script = list(dup)
        try:
            itom.resolve("guided", "custom", "鲁红文")
        except itom.ItomError as e:
            assert "同名记录" in str(e) and "焦化事业部" in str(e), str(e)[:200]
        else:
            raise AssertionError("重名应报歧义")
        fake.script = list(dup)
        picked = itom.resolve("guided", "custom", "鲁红文", hint="原燃料检测中心")
        assert picked["sid"] == 2, picked

        # 4) 完整引导装配：逐级带父级请求，payload 覆盖平台必填列
        fake.script = [
            rows({"orgCode": "600003010102", "orgDesc": "智能应用事业部", "orgSid": 424}),
            rows({"orgCode": "60000301010201", "orgDesc": "信息化维护班", "orgSid": 425}),
            rows({"sysSid": 33298, "sysCode": "SG057", "sysName": "实验室管理系统", "sysLevel": 1}),
            rows({"sysSid": 33311, "sysCode": "SG057003", "sysName": "原料分析中心", "sysLevel": 2}),
            rows({"sysSid": 33375, "sysCode": "SG057003001", "sysName": "外部委托查询", "sysLevel": 3}),
            rows({"sid": 2, "customName": "鲁红文", "mobilePhone": "17708580769",
                  "departentName": "质检监督部", "operationName": "原燃料检测中心", "postName": "办公室"}),
            rows({"userId": "wuxy1613", "userName": "吴希禹", "mobilePhone": "18786696624",
                  "sysUserType": 1}),
            rows({"orgCode": "600031", "orgDesc": "质检监督部", "orgSid": 646}),
        ]
        pre = itom.prepare_incident(
            "guided", dept="智能应用事业部", group="信息化维护班", system="实验室管理系统",
            subclass="原料分析中心", menu="外部委托查询", reporter="鲁红文", level="低",
            nature="故障", role="一线人员", handler="吴希禹", description="权限查不到质检信息")
        payload = pre["payload"]
        assert (payload["deptCode"], payload["groupSid"], payload["systemTypeSid"]) == (
            "600003010102", 425, 33298), payload
        assert (payload["systemSubclassSid"], payload["repairMenu"], payload["dealStaff"]) == (
            33311, "SG057003001", "wuxy1613"), payload
        assert (payload["eventLevel"], payload["eventNature"], payload["dealStaffRole"]) == (
            "1", "1", "1"), payload
        assert payload["repairDeptCode"] == "600031" and not pre["warnings"], pre["warnings"]
        assert payload["repairStaffPhone"] == "17708580769" and payload["dealStaffPhone"] == "18786696624"
        # 父级顺序：group 带部门码、system 带组码、subclass/menu 带上级 sysSid
        bodies = [c["json"]["reqBody"] for c in fake.calls[-8:]]
        assert bodies[1]["orgCode"] == "600003010102" and bodies[2]["groupCode"] == "60000301010201"
        assert bodies[3]["parentSid"] == 33298 and bodies[4]["parentSid"] == 33311
        assert bodies[6]["sysSid"] == 33298 and bodies[6]["sysUserType"] == "1"

        # 5) 报修部门解析不出来时进 warnings（不阻断预览，但阻断 confirm 提交）
        fake.script = [
            rows({"orgCode": "600003010102", "orgDesc": "智能应用事业部", "orgSid": 424}),
            rows({"orgCode": "60000301010201", "orgDesc": "信息化维护班", "orgSid": 425}),
            rows({"sysSid": 33298, "sysCode": "SG057", "sysName": "实验室管理系统", "sysLevel": 1}),
            rows({"sysSid": 33311, "sysCode": "SG057003", "sysName": "原料分析中心", "sysLevel": 2}),
            rows({"sysSid": 33375, "sysCode": "SG057003001", "sysName": "外部委托查询", "sysLevel": 3}),
            rows({"sid": 2, "customName": "张三", "mobilePhone": "1772", "departentName": "质检监督部"}),
            rows({"userId": "wuxy1613", "userName": "吴希禹", "mobilePhone": "187", "sysUserType": 1}),
            # 报修部门按名称查到两条同名不同级 → 不猜，进 warnings
            rows({"orgCode": "600031", "orgDesc": "质检监督部", "orgSid": 646, "parentOrgCode": "600"},
                 {"orgCode": "30003001", "orgDesc": "质检监督部", "orgSid": 9, "parentOrgCode": "30000001"}),
        ]
        pre2 = itom.prepare_incident(
            "guided", dept="智能应用事业部", group="信息化维护班", system="实验室管理系统",
            subclass="原料分析中心", menu="外部委托查询", reporter="张三", level="低",
            nature="故障", role="一线人员", handler="吴希禹", description="x")
        assert pre2["payload"]["repairDeptCode"] is None
        assert any("报修部门代码未解析" in w for w in pre2["warnings"]), pre2["warnings"]
    finally:
        itom.httpx2 = real_http
        itom._sessions.clear()
        itom._dict_cache.pop("EVENT_LEVEL", None)
        itom._dict_cache.pop("EVENT_NATURE", None)
        itom._dict_cache.pop("SYS_USER_TYPE", None)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("PASS  it_ops·ITOM 引导式建单（父级级联/重名回问/敏感列剔除/payload 对齐平台）")


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
        assert timeout_for("data_slow_tool") == 3.0 and timeout_for("other") == 120.0, \
            "默认下游超时应为规范要求的 120s"
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


def test_tool_governance() -> None:
    """工具数量治理回归：本地台账/数仓/批处理演示工具与 common 测试工具不再注册。"""
    from mcp_common_server import main as common
    from mcp_itops import main as itops

    itops_names = asyncio.run(_list_tools(itops.mcp))
    removed = {"itops_create_incident", "itops_list_incidents", "itops_update_incident_status",
               "itops_create_change", "itops_get_change", "itops_register_asset",
               "itops_list_assets", "itops_export_assets_to_warehouse",
               "itops_query_warehouse_assets", "itops_batch_process", "itops_list_datasets",
               "itops_query_dataset", "itops_list_data_tables", "itops_itom_login"}
    assert not (itops_names & removed), f"已下线工具仍在注册: {itops_names & removed}"
    assert len(itops_names) <= 8, f"领域工具数超上限: {sorted(itops_names)}"

    common_names = asyncio.run(_list_tools(common.mcp))
    assert "echo" not in common_names and "timestamp" not in common_names, common_names
    print(f"PASS  工具治理·it_ops={len(itops_names)} 个 / common={len(common_names)} 个（测试工具已清退）")


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
    test_itom_account_ref_client()
    test_itom_platform_contract()
    test_itom_guided_incident_build()
    test_tool_governance()
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
