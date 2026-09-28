"""运维工具测试集（真实部署链路）。

用 tests/ops/client.py 通过 Streamable HTTP 连真实进程，覆盖日常运维最关心的四类场景：

- check   ：连通性 + 工具清单（三层是否都活着、暴露了哪些工具）
- smoke   ：各层核心工具功能验证（读/写/通用工具）
- approve ：HITL 审批闸门完整流（挂起 → 批准执行 / 驳回不执行 / 令牌保护 / 幂等重放）
- guard   ：企业化守护（鉴权开关、审计、结果上限、路径围栏、SQL 只读、任务列表）
- load    ：批量压测（可选，验证并发与稳定性）

用法（先按 scripts/run-demo.ps1 起服务）：
    python tests/ops/run_ops_test.py                 # 跑 check + smoke + approve
    python tests/ops/run_ops_test.py --suite check   # 只跑连通性
    python tests/ops/run_ops_test.py --load 50        # 附带 50 次并发压测
    python tests/ops/run_ops_test.py --url http://127.0.0.1:9000/mcp   # 自定义网关地址

退出码：0=全部通过；1=有失败（可直接用于 CI / 巡检脚本）。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
import time
import traceback
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 开发期未 pip install 时也能 import（editable 安装后无副作用）
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from ops import client  # noqa: E402

# 统一敏感配置入口：standalone 运行时也从 config/platform.env 取远端 Go 的 URL/TOKEN
try:
    from mcp_shared.config import load_platform_env  # noqa: E402
    load_platform_env()
except ImportError:
    pass

_godh_token = os.environ.get("MCP_GODATAHUB_TOKEN")
GODH_HEADERS = {"Authorization": f"Bearer {_godh_token}"} if _godh_token else None

# 网关自身 Bearer（配了 MCP_GATEWAY_TOKEN 才生效；审批令牌 MCP_APPROVAL_TOKEN 另给审批用例用）
_gw_token = os.environ.get("MCP_GATEWAY_TOKEN")
GW_HEADERS = {"Authorization": f"Bearer {_gw_token}"} if _gw_token else None
APPROVAL_TOKEN = os.environ.get("MCP_APPROVAL_TOKEN")
# 审批人通道：令牌走请求头（推荐做法——模型不需要、也不应知道令牌）
APPROVER_HEADERS = {**(GW_HEADERS or {}), "X-Sg-Approval-Token": APPROVAL_TOKEN,
                    "X-Sg-Actor": os.environ.get("MCP_OPS_APPROVER", "ops-approver")} if APPROVAL_TOKEN else GW_HEADERS

# ---------------------------------------------------------------------------
# 测试框架（极简：不引入 pytest，便于运维直接在目标机跑）
# ---------------------------------------------------------------------------
_results: list[tuple[str, bool, str]] = []
args_url: dict[str, str] = {}  # main() 填充：网关地址给上面的包装函数用


def _record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f"  — {detail}"
    print(line, flush=True)


def gw_call(tool: str, args: dict | None = None, headers: dict | None = None):
    """经网关调用（自动带上 MCP_GATEWAY_TOKEN 的 Bearer 头；审批类调用传 headers=APPROVER_HEADERS）。"""
    return client.call(args_url["gw"], tool, args, headers=headers or GW_HEADERS)


def gw_call_raw(tool: str, args: dict | None = None, headers: dict | None = None):
    return client.call_raw(args_url["gw"], tool, args, headers=headers or GW_HEADERS)


def gw_list_tools() -> list[str]:
    return client.list_tools(args_url["gw"], headers=GW_HEADERS)


def check(name: str, fn) -> None:
    """跑一个用例，捕获所有异常记 FAIL，不中断后续用例。"""
    try:
        detail = fn()
        _record(name, True, detail or "")
    except Exception as e:  # noqa: BLE001 — 测试框架必须兜住一切
        _record(name, False, f"{type(e).__name__}: {e}")
        if "-v" in sys.argv:
            traceback.print_exc()


# ---------------------------------------------------------------------------
# 1) 连通性检查
# ---------------------------------------------------------------------------
def suite_check(gw: str, itops: str, common: str, godh: str | None) -> None:
    print("== 1. 连通性 & 工具清单 ==", flush=True)

    def gateway_tools():
        names = gw_list_tools()
        assert "gateway_call" in names, f"网关缺少统一入口工具，实为 {names}"
        assert "list_routes" in names, f"网关缺少工具发现入口 list_routes，实为 {names}"
        assert "gateway_call_kv" not in names, "重复入口 gateway_call_kv 应已并入 gateway_call"
        # 聚合暴露模型：每 server 一个 route_* 工具（method 枚举），不逐个展示下游工具
        routes = {n for n in names if n.startswith("route_")}
        assert {"route_it_ops", "route_common_tools"} <= routes, f"缺少聚合路由工具: {routes}"
        assert not (set(names) & set(client.list_tools(itops))), "下游工具不应逐个出现在网关 tools/list"
        return f"{len(names)} 个工具: {', '.join(sorted(names))}"

    def itops_tools():
        names = client.list_tools(itops)
        # 第 1 层·领域工具（itops.* 前缀）+ 第 2 层·领域通用查询
        for expect in ("itops_create_incident", "itops_list_incidents",
                       "itops_create_change", "itops_register_asset",
                       "itops_export_assets_to_warehouse", "itops_query_warehouse_assets",
                       "itops_batch_process", "itops_list_datasets",
                       "itops_query_dataset", "itops_list_data_tables"):
            assert expect in names, f"it_ops 缺少 {expect}"
        return f"{len(names)} 个工具（itops.* 领域前缀）"

    def common_tools():
        names = client.list_tools(common)
        for expect in ("now", "echo", "generate_id", "slugify", "timestamp"):
            assert expect in names, f"common 缺少 {expect}"
        return f"{len(names)} 个工具"

    check("网关可达 + 暴露统一入口", gateway_tools)
    check("it_ops 可达 + 工具齐全", itops_tools)
    check("common-tools 可达 + 工具齐全", common_tools)

    # 聚合器一致性：网关 list_routes 必须等于「直连各下游 tools/list」的并集
    # （网关启动时作为 MCP Client 拉取下游工具表合并，见 mcp_gateway.aggregator）
    def gateway_aggregates():
        routes = gw_call("list_routes", {})
        pairs = {(r["server"], r["tool"]) for r in routes}
        assert all("params" in r for r in routes), "聚合路由应携带下游入参摘要（完整 schema 走 full_schema）"
        direct = {("it_ops", n) for n in client.list_tools(itops)} | {
            ("common-tools", n) for n in client.list_tools(common)
        }
        missing = direct - pairs
        assert not missing, f"下游有而网关未聚合: {missing}（下游刚上线？让网关调 refresh_routes）"
        return f"{len(pairs)} 条路由 = it_ops/common 直连并集"

    check("网关聚合路由 = 下游 tools/list 合并", gateway_aggregates)

    if godh:
        def godh_tools():
            names = client.list_tools(godh, headers=GODH_HEADERS)
            # 架构约定：重活层对 AI 暴露的工具统一 data. 前缀
            for expect in ("data_list_sources", "data_submit_collect_job", "data_get_job_status",
                           "data_db_ping", "data_list_dsn_refs", "data_list_tables",
                           "data_batch_import", "data_batch_query",
                           "data_batch_process", "data_list_datasets", "data_query_dataset", "data_db_query_preview",
                           "data_hash_file", "data_file_stats", "data_convert_file"):
                assert expect in names, f"go_datahub 缺少 {expect}"
            return f"{len(names)} 个工具（data.* 全局数据平台前缀）"

        check("go_datahub 可达 + 工具齐全", godh_tools)


# ---------------------------------------------------------------------------
# 2) 冒烟：各层核心功能
# ---------------------------------------------------------------------------
def suite_smoke(gw: str, itops: str, common: str, godh: str | None) -> None:
    print("== 2. 核心工具功能 ==", flush=True)

    def common_echo():
        r = client.call(common, "echo", {"message": "ops-check"})
        assert r == "ops-check", r
        return r

    def common_echo_upper():
        r = client.call(common, "echo", {"message": "abc", "uppercase": True})
        assert r == "ABC", r
        return r

    def common_slugify():
        r = client.call(common, "slugify", {"text": "Deploy API v2!"})
        assert r == "deploy-api-v2", r
        return r

    def common_now_shanghai():
        r = client.call(common, "now", {"timezone_name": "Asia/Shanghai"})
        assert "+08:00" in r, f"时区不正确: {r}"
        return r

    def common_id_unique():
        a = client.call(common, "generate_id", {"prefix": "test"})
        b = client.call(common, "generate_id", {"prefix": "test"})
        assert a != b and a.startswith("test_"), (a, b)
        return a

    check("common.echo 回显", common_echo)
    check("common.echo 大写模式", common_echo_upper)
    check("common.slugify 命名转换", common_slugify)
    check("common.now 时区（Asia/Shanghai）", common_now_shanghai)
    check("common.generate_id 唯一性", common_id_unique)

    # it_ops 直连（绕过网关，验证中层自身可用）
    def itops_create_incident():
        r = client.call(itops, "itops_create_incident", {"title": "运维测试工单", "priority": "high"})
        assert r["id"].startswith("INC-"), r
        return f"{r['id']} status={r['status']}"

    def itops_status_flow():
        created = client.call(itops, "itops_create_incident", {"title": "状态流转测试"})
        iid = created["id"]
        updated = client.call(itops, "itops_update_incident_status", {"incident_id": iid, "status": "in_progress"})
        assert updated["status"] == "in_progress", updated
        listing = client.call(itops, "itops_list_incidents", {"status": "in_progress"})
        ids = [i["id"] for i in listing] if isinstance(listing, list) else []
        assert iid in ids, f"过滤结果未包含 {iid}"
        return f"{iid} new → in_progress 并可按状态过滤"

    def itops_asset():
        r = client.call(itops, "itops_register_asset", {"name": "srv-ops-01", "asset_type": "server", "owner": "ops"})
        assert r["id"].startswith("AST-"), r
        listed = client.call(itops, "itops_list_assets", {"asset_type": "server"})
        assert any(a["id"] == r["id"] for a in listed), "资产未出现在列表中"
        return f"{r['id']} 已登记并可查询"

    check("it_ops 创建工单", itops_create_incident)
    check("it_ops 工单状态流转 + 过滤", itops_status_flow)
    check("it_ops 资产登记 + 查询", itops_asset)

    # 通过网关的统一入口（只读，直接放行）
    def gw_readonly_pass():
        r = gw_call( "gateway_call", {
            "server": "common-tools", "tool": "echo", "arguments": {"message": "via-gateway"},
        })
        assert r["approved"] is True and r["executed"] is True, r
        assert r["result"] == "via-gateway", r
        return "只读工具经网关直接放行"

    def gw_itops_readonly():
        r = gw_call( "gateway_call", {
            "server": "it_ops", "tool": "itops_list_incidents", "arguments": {},
        })
        assert r["executed"] is True and isinstance(r["result"], list), r
        return f"经网关读取工单 {len(r['result'])} 条"

    check("网关放行只读工具（common）", gw_readonly_pass)
    check("网关路由只读工具（it_ops）", gw_itops_readonly)

    if godh:
        # go_datahub 直连：异步 job 全流程（提交 → 轮询 → 完成）
        def godh_job_flow():
            srcs = client.call(godh, "data_list_sources", headers=GODH_HEADERS)
            names = [s["name"] for s in srcs["sources"]]
            assert "synthetic" in names, names
            sub = client.call(godh, "data_submit_collect_job", {
                "source": "synthetic", "params": {"rows": 2000, "batch_pause_ms": 0},
            }, headers=GODH_HEADERS)
            jid = sub["job_id"]
            assert jid.startswith("job-"), sub
            deadline = time.time() + 20
            while True:
                st = client.call(godh, "data_get_job_status", {"job_id": jid}, headers=GODH_HEADERS)
                if st["status"] in ("completed", "failed", "cancelled"):
                    break
                if time.time() > deadline:
                    raise RuntimeError(f"job 超时: {st}")
                time.sleep(0.2)
            assert st["status"] == "completed" and st["rows"] == 2000, st
            return f"{jid} 采集 2000 行完成"

        def godh_db_ping_error():
            r = client.call(godh, "data_db_ping", {"db_type": "mysql", "dsn": "root:x@tcp(127.0.0.1:1)/none"},
                            headers=GODH_HEADERS)
            assert r["ok"] is False and r["error"], r
            return "不可达 DSN 返回结构化 ok=false"

        def godh_dsn_ref_error_path():
            """未知 dsn_ref 必须是可读的工具错误，且不得泄露任何已配置连接串。"""
            r = client.call_raw(godh, "data_db_ping", {"db_type": "pg", "dsn_ref": "no_such_ref"},
                                headers=GODH_HEADERS)
            assert r.is_error, "未知 dsn_ref 竟然成功"
            text = str(r.content)
            assert "://" not in text, "错误信息疑似泄露连接串"
            return "未知 dsn_ref 报可读错误且不泄露凭证"

        def gw_godh_readonly():
            r = gw_call( "gateway_call", {
                "server": "go_datahub", "tool": "data_list_sources", "arguments": {},
            })
            assert r["executed"] is True and isinstance(r["result"], dict), r
            return "经网关 HTTP 转发读取 go_datahub 采集器清单"

        # 库表操作（data.list_tables / batch_import / batch_query）：真库成功路径
        # 需要真实 DSN；此处验证「不可达库 → 结构化 ok=false 且不泄露连接串」
        def godh_list_tables_error():
            r = client.call(godh, "data_list_tables",
                            {"db_type": "pg", "dsn": "postgres://u:p@127.0.0.1:1/none"},
                            headers=GODH_HEADERS)
            assert r["ok"] is False and r["error"], r
            assert "://" not in r["error"], "错误信息疑似泄露连接串"
            return "data_list_tables 不可达库返回结构化 ok=false"

        def godh_batch_import_param_error():
            """非法表名必须是可读工具错误（参数校验在连库之前）。"""
            r = client.call_raw(godh, "data_batch_import", {
                "db_type": "pg", "dsn": "postgres://u:p@127.0.0.1:1/none",
                "table": "t; DROP TABLE x", "rows": [{"a": 1}],
            }, headers=GODH_HEADERS)
            assert r.is_error, "非法表名竟然成功"
            return "data_batch_import 非法表名被白名单拦截"

        # 业务层内部 MCP 直调示例：经网关调 itops.export_assets_to_warehouse，
        # it_ops 内部再经 MCP 协议直连 go_datahub（gateway → it_ops → go_datahub 三跳）
        def gw_business_to_heavy():
            r = gw_call_raw( "gateway_call", {
                "server": "it_ops", "tool": "itops_export_assets_to_warehouse",
                "arguments": {"dsn_ref": "no_such_ref"},
            })
            # 未知 dsn_ref：重活层内部端点 400 → it_ops 转可读 ToolError → 网关 is_error
            assert r.is_error, "未知 dsn_ref 竟然成功"
            text = str(r.content)
            assert "://" not in text, "错误信息疑似泄露连接串"
            return "business→heavy 内部 REST 直调链路可达（错误路径验证）"

        # 架构示例全流程（经网关）：AI 调 data.batch_process(业务名 orders,
        # 2024-05, 租户 t1) → 网关转发 Go MCP → 内部按租户路由库、按月分表 →
        # 异步执行返回 task_id → 轮询完成
        def gw_heavy_batch_process_flow():
            sub = gw_call( "gateway_call", {
                "server": "go_datahub", "tool": "data_batch_process",
                "arguments": {"dataset_id": "orders", "period": "2024-05",
                              "tenant_id": "t1", "rows": 100},
            })
            r = sub["result"]
            assert r["task_id"].startswith("job-"), sub
            assert r["routed"]["table"] == "orders_202405", sub
            assert r["routed"]["db"] == "order_t1_pg", f"租户路由应命中 DSN_T1: {sub}"
            deadline = time.time() + 15
            st = {}
            while True:
                st = gw_call( "gateway_call", {
                    "server": "go_datahub", "tool": "data_get_job_status",
                    "arguments": {"job_id": r["task_id"]},
                })["result"]
                if st["status"] in ("completed", "failed", "cancelled"):
                    break
                if time.time() > deadline:
                    raise RuntimeError(f"批处理任务超时: {st}")
                time.sleep(0.2)
            assert st["status"] == "completed", st
            assert "table=orders_202405" in st["message"] and "db=order_t1_pg" in st["message"], st
            return f"{r['task_id']} 路由 db=order_t1_pg table=orders_202405 simulate 完成"

        # Python 侧同款（经网关）：itops.batch_process 在业务层做路由，
        # real 模式经内部 REST 下放重活层执行
        def gw_business_batch_process():
            r = gw_call( "gateway_call", {
                "server": "it_ops", "tool": "itops_batch_process",
                "arguments": {"dataset_id": "orders", "period": "2024-05",
                              "tenant_id": "t1", "mode": "simulate"},
            })
            d = r["result"]
            assert d["task_id"].startswith("task-") and d["routed"]["table"] == "orders_202405", r
            assert d["routed"]["db"] == "order_t1_pg", r
            # real 模式：本机无真实库 → 重活层结构化 ok=false 转可读工具错误
            bad = gw_call_raw( "gateway_call", {
                "server": "it_ops", "tool": "itops_batch_process",
                "arguments": {"dataset_id": "orders", "period": "2024-05",
                              "tenant_id": "t1", "mode": "real"},
            })
            assert bad.is_error, "real 模式无真实库竟然成功"
            return f"Python 路由一致（{d['task_id']}），real 无库报可读错误"

        # 事件单 api 通道：未配置外部 ITSM → 可读错误（引导改用 local/sql）
        def gw_incident_api_channel_error():
            r = gw_call_raw( "gateway_call", {
                "server": "it_ops", "tool": "itops_create_incident",
                "arguments": {"title": "API通道测试", "channel": "api"},
            })
            assert r.is_error, "未配置 ITSM 竟然成功"
            assert "MCP_ITSM_API_URL" in str(r.content), r
            return "事件单 api 通道未配置时报可读错误"

        # 数据集清单：AI 的批处理/查询前置发现入口（两侧清单一致 + 域归属/中文说明随行）
        def gw_list_datasets():
            r = gw_call( "gateway_call", {
                "server": "go_datahub", "tool": "data_list_datasets", "arguments": {},
            })
            infos = {d["dataset"]: d for d in r["result"]["datasets"]}
            assert {"orders", "incidents", "assets"} <= set(infos), r
            assert infos["orders"]["domain"] == "order" and infos["orders"]["desc"], r
            assert infos["incidents"]["domain"] == "itops", r
            r2 = gw_call( "gateway_call", {
                "server": "it_ops", "tool": "itops_list_datasets", "arguments": {},
            })
            names2 = {d["dataset"] for d in r2["result"]["datasets"]}
            assert names2 == set(infos), f"两侧路由清单应一致: {set(infos)} vs {names2}"
            return f"两侧一致：{sorted(infos)}（含 domain/desc）"

        # 第 3 层·全局数据平台：data.query_dataset 全量数据集枚举（含 order 域）
        def gw_data_query_dataset():
            r = gw_call_raw( "gateway_call", {
                "server": "go_datahub", "tool": "data_query_dataset",
                "arguments": {"dataset_id": "incidents", "tenant_id": "default"},
            })
            # 本机无真实库 → 结构化 ok=false（路由成功，库不可达），错误脱敏
            assert r.is_error or "://" not in str(r.content), r
            d = gw_call_raw( "gateway_call", {
                "server": "go_datahub", "tool": "data_query_dataset",
                "arguments": {"dataset_id": "orders", "tenant_id": "t1", "period": "2024-05"},
            })
            assert d.is_error, "无真实库竟然查询成功"
            assert "://" not in str(d.content), "错误信息疑似泄露连接串"
            return "data_query_dataset 路由解析可达（无库走结构化失败路径）"

        # 第 2 层·领域通用查询：itops.query_dataset 域限定（越域引导去第 3 层）
        def gw_itops_query_dataset_domain_guard():
            # 越域：orders 属 order 域 → 可读错误引导用 data.query_dataset
            r = gw_call_raw( "gateway_call", {
                "server": "it_ops", "tool": "itops_query_dataset",
                "arguments": {"dataset_id": "order"},  # 枚举外值，模拟越域调用
            })
            assert r.is_error, "越域数据集竟然成功"
            return "itops_query_dataset 域校验拦截（枚举 + 越域引导）"

        check("go_datahub 异步 job 全流程", godh_job_flow)
        check("go_datahub db_ping 失败路径", godh_db_ping_error)
        check("go_datahub 未知 dsn_ref 不泄露凭证", godh_dsn_ref_error_path)
        check("go_datahub list_tables 失败路径", godh_list_tables_error)
        check("go_datahub batch_import 表名白名单", godh_batch_import_param_error)
        check("网关 HTTP 转发到 go_datahub（只读）", gw_godh_readonly)
        check("business→heavy 内部 REST 直调链路", gw_business_to_heavy)
        check("data_batch_process 业务名路由全流程", gw_heavy_batch_process_flow)
        check("itops_batch_process Python 侧路由", gw_business_batch_process)
        check("事件单 api 通道未配置报可读错误", gw_incident_api_channel_error)
        check("数据集清单两侧一致（含域归属/说明）", gw_list_datasets)
        check("data_query_dataset 全局细粒度查询", gw_data_query_dataset)
        check("itops_query_dataset 领域枚举限定", gw_itops_query_dataset_domain_guard)


# ---------------------------------------------------------------------------
# 3) 审批闸门（HITL）
# ---------------------------------------------------------------------------
def suite_approve(gw: str, _itops: str, _common: str, godh: str | None = None) -> None:
    print("== 3. 审批闸门（HITL）==", flush=True)
    marker = f"运维审批测试-{uuid.uuid4().hex[:6]}"

    # 挂起：高风险写操作不应立即执行
    pending: dict = {}

    def gw_change_pending():
        r = gw_call( "gateway_call", {
            "server": "it_ops", "tool": "itops_create_change",
            "arguments": {"title": marker, "risk": "high"},
            "requested_by": "ops-test",
        })
        assert r["approved"] is False and r["executed"] is False, r
        assert r["request_id"].startswith("apr_"), r
        pending["id"] = r["request_id"]
        return f"已挂起 request_id={r['request_id']}"

    check("高风险写操作被挂起（不执行）", gw_change_pending)

    def gw_visible_in_pending():
        items = gw_call( "list_pending_approvals", {})
        ids = [i["id"] for i in items]
        assert pending.get("id") in ids, f"待办列表未包含 {pending.get('id')}"
        return f"待审批 {len(items)} 条，含本次请求"

    check("审批单出现在待办列表", gw_visible_in_pending)

    # 批准 → 真正执行到下游
    def gw_approve_executes():
        r = gw_call("approve_request", {"request_id": pending["id"], "comment": "运维测试放行"},
                    headers=APPROVER_HEADERS)
        assert r["executed"] is True, r
        assert r["status"] == "approved", r
        assert "CHG-" in str(r["result"]), r
        pending["change_id"] = r["result"]["id"]
        return f"已执行，产出变更单 {r['result']['id']}"

    check("批准后执行下游并产出变更单", gw_approve_executes)

    def gw_change_readable():
        r = gw_call( "gateway_call", {
            "server": "it_ops", "tool": "itops_get_change",
            "arguments": {"change_id": pending["change_id"]},
        })
        assert r["result"]["title"] == marker, r
        return "变更单可经网关查回，标题一致"

    check("审批产物可回查", gw_change_readable)

    # 驳回 → 不执行
    rejected: dict = {}

    def gw_reject_no_execute():
        r = gw_call( "gateway_call", {
            "server": "it_ops", "tool": "itops_create_change", "arguments": {"title": f"{marker}-驳回"},
        })
        rid = r["request_id"]
        d = gw_call("reject_request", {"request_id": rid, "comment": "风险过高"}, headers=APPROVER_HEADERS)
        assert d["executed"] is False and d["status"] == "rejected", d
        rejected["id"] = rid
        return "驳回后未执行任何下游调用"

    check("驳回后不执行", gw_reject_no_execute)

    def gw_redecide_blocked():
        """已决审批单不能再次决策（状态机保护）。"""
        result = gw_call_raw("approve_request", {"request_id": rejected["id"]}, headers=APPROVER_HEADERS)
        assert result.is_error, "已驳回的审批单竟可再次批准"
        return "已决审批单拒绝重复决策"

    check("已决审批单不可重复决策", gw_redecide_blocked)

    if godh:
        # 批量采集提交（Go 重活）经审批闸门：挂起 → 批准 → job 真正被创建
        def gw_godh_submit_approval():
            r = gw_call( "gateway_call", {
                "server": "go_datahub", "tool": "data_submit_collect_job",
                "arguments": {"source": "synthetic", "params": {"rows": 100}},
            })
            assert r["approved"] is False and r["executed"] is False, r
            d = gw_call("approve_request", {"request_id": r["request_id"], "comment": "运维测试放行采集"},
                       headers=APPROVER_HEADERS)
            assert d["executed"] is True, d
            job_id = d["result"]["job_id"]
            assert job_id.startswith("job-"), d
            st = gw_call( "gateway_call", {
                "server": "go_datahub", "tool": "data_get_job_status", "arguments": {"job_id": job_id},
            })
            assert st["result"]["status"] in ("pending", "running", "completed"), st
            return f"审批放行后 Go 侧创建任务 {job_id}"

        check("go_datahub 采集任务经审批闸门执行", gw_godh_submit_approval)

    # 未注册路由 → 协议错误
    def gw_unknown_route():
        result = gw_call_raw( "gateway_call", {"server": "nope", "tool": "x", "arguments": {}})
        assert result.is_error, "未注册路由竟然成功"
        return "未注册路由返回协议错误"

    check("未注册路由报错", gw_unknown_route)


# ---------------------------------------------------------------------------
# 5) 企业化守护（上生产前必查）
# ---------------------------------------------------------------------------
def suite_guard(gw: str, _itops: str, _common: str, godh: str | None) -> None:
    print("== 5. 企业化守护 ==", flush=True)

    def gw_healthz():
        info = client.healthz(gw)
        assert info["ok"] is True and info.get("server"), info
        return f"网关 /healthz ok（uptime {info.get('uptime_s')}s，routes {info.get('routes')}）"

    check("网关 /healthz 探活可用", gw_healthz)

    def gw_security_switches():
        sec = gw_call("describe_gateway", {})["security"]
        for key in ("approval_token_required", "gateway_bearer_enabled",
                    "downstream_timeout_seconds", "max_result_items", "max_result_chars"):
            assert key in sec, (key, sec)
        assert sec["downstream_timeout_seconds"] > 0 and sec["max_result_items"] > 0, sec
        if not sec["approval_token_required"]:
            print("      [提示] 未配 MCP_APPROVAL_TOKEN：AI 可自行 approve_request，生产必须配")
        if not sec["gateway_bearer_enabled"]:
            print("      [提示] 未配 MCP_GATEWAY_TOKEN：网关端口无鉴权，生产必须配")
        return (f"审批令牌={sec['approval_token_required']} Bearer={sec['gateway_bearer_enabled']} "
                f"下游超时={sec['downstream_timeout_seconds']}s 结果上限={sec['max_result_items']}条")

    check("describe_gateway 回报生效的安全开关", gw_security_switches)

    def gw_downstreams_health():
        items = gw_call("list_downstreams", {})
        assert items and all("tools_aggregated" in i for i in items), items
        bad = [i for i in items if i["status"] != "ok"]
        assert not bad, f"有下游未聚合成功: {[(b['name'], b['status']) for b in bad]}"
        return "全部下游 ok：" + ", ".join(f"{i['name']}({i['tools_aggregated']}工具)" for i in items)

    check("list_downstreams 聚合健康度", gw_downstreams_health)

    def gw_catalog_compact():
        import json as _json

        compact = gw_call("list_tool_catalog", {})
        full = gw_call("list_tool_catalog", {"mode": "full"})
        c_len, f_len = len(_json.dumps(compact, ensure_ascii=False)), len(_json.dumps(full, ensure_ascii=False))
        assert c_len < f_len, f"compact 应比 full 省 token：{c_len} vs {f_len}"
        assert all(g.get("tools") for g in compact), compact
        assert all("params" in t for g in full for t in g["tools"]), "full 模式应含参数明细"
        only = gw_call("list_tool_catalog", {"server": "it_ops"})
        assert len(only) == 1 and only[0]["mcp_server"] == "it_ops", only
        return f"目录体积保护（compact {c_len} < full {f_len} 字符）+ server 过滤"

    check("list_tool_catalog 精简默认与过滤", gw_catalog_compact)

    def gw_routes_summary():
        import json as _json

        one = gw_call("list_routes", {"server": "it_ops"})
        assert one and all(r["server"] == "it_ops" for r in one), one
        assert all("params" in r and "input_schema" not in r for r in one), "默认应给参数摘要"
        small = len(_json.dumps(one, ensure_ascii=False))
        big = len(_json.dumps(gw_call("list_routes", {}), ensure_ascii=False))
        full = gw_call("list_routes", {"server": "it_ops", "full_schema": True})
        assert all(r.get("input_schema") for r in full), "full_schema=True 才回完整 schema"
        assert small < big, "过滤后体积应更小"
        return f"摘要/完整/过滤三态生效（it_ops {len(one)} 条 {small} 字 < 全量 {big} 字）"

    check("list_routes 摘要形态与体积保护", gw_routes_summary)

    def gw_route_method_enum():
        assert "route_it_ops" in gw_list_tools(), gw_list_tools()
        bad = gw_call_raw("route_it_ops", {"method": "definitely_not_a_method", "params": {}})
        assert bad.is_error, "枚举外 method 必须被 schema 拒绝"
        methods = {r["tool"] for r in gw_call("list_routes", {"server": "it_ops"})}
        return f"route_it_ops 枚举 {len(methods)} 个方法，脏 method 被拒"

    check("route_* method 枚举约束", gw_route_method_enum)

    def gw_idempotency():
        key = f"ops-idem-{uuid.uuid4().hex[:8]}"
        title = f"幂等守护用例-{key}"   # 标题唯一：SQLite 跨轮次持久化，固定标题会与历史数据撞车
        a = gw_call("gateway_call", {"server": "it_ops", "tool": "itops_create_incident",
                                     "arguments": {"title": title}, "idempotency_key": key})
        b = gw_call("gateway_call", {"server": "it_ops", "tool": "itops_create_incident",
                                     "arguments": {"title": title}, "idempotency_key": key})
        assert a["result"]["id"] == b["result"]["id"], (a, b)
        assert b.get("idempotent_replay") is True, b
        rows = gw_call("gateway_call", {"server": "it_ops", "tool": "itops_list_incidents",
                                        "arguments": {}})["result"]
        rows = rows["result"] if isinstance(rows, dict) and "result" in rows else rows
        hits = [r for r in rows if r.get("title") == title]
        assert len(hits) == 1, f"同一幂等键重复落库 {len(hits)} 次"
        return f"重试不重复执行（{a['result']['id']} 仅一条）"

    check("写操作 idempotency_key 防重复执行", gw_idempotency)

    def gw_audit_traceable():
        call_id = gw_call("gateway_call", {"server": "common-tools", "tool": "echo",
                                           "arguments": {"message": "audit-me"}})["call_id"]
        events = gw_call("query_audit_log", {"limit": 30, "call_id": call_id})
        assert events, f"审计中查不到 call_id={call_id}"
        e = events[0]
        assert e["event"] == "tool_call" and e["ok"] is True and "ms" in e, e
        denied = gw_call("query_audit_log", {"limit": 5, "tool": "describe_gateway"})
        assert isinstance(denied, list), denied
        return f"审计可按 call_id 回查（{e['server']}.{e['tool']} {e['ms']}ms）"

    check("调用审计落盘并可回查", gw_audit_traceable)

    def gw_error_actionable():
        r = gw_call_raw("gateway_call", {"server": "it_ops", "tool": "create_inciden",
                                         "arguments": {"title": "typo"}})
        assert r.is_error, "错工具名应报错"
        text = str(r.content)
        assert "itops_create_incident" in text, f"应给出相近工具名: {text[:200]}"
        assert "list_routes" in text or "refresh_routes" in text, "应给下一步指引"
        return "错误含相近工具名 + 下一步指引（省 AI 往返）"

    check("路由未命中给可执行建议", gw_error_actionable)

    def gw_approval_token_enforced():
        """配了 MCP_APPROVAL_TOKEN 时的真实行为：AI 侧无令牌不能自批，审批人带令牌可放行。"""
        if not APPROVAL_TOKEN:
            return "跳过（未配 MCP_APPROVAL_TOKEN；生产必须配，见 develop-deploy §8.1）"
        marker = f"令牌守护-{uuid.uuid4().hex[:6]}"
        rid = gw_call("gateway_call", {"server": "it_ops", "tool": "itops_create_change",
                                       "arguments": {"title": marker, "risk": "high"}})["request_id"]
        bad = gw_call_raw("approve_request", {"request_id": rid})
        assert bad.is_error and "审批令牌" in str(bad.content), "配了令牌却可被无令牌放行（HITL 失效）"
        # 审批人通道：令牌走请求头，模型不需要知道
        hdr = {**(GW_HEADERS or {}), "X-Sg-Approval-Token": APPROVAL_TOKEN, "X-Sg-Actor": "ops-approver"}
        ok = client.call(gw, "approve_request", {"request_id": rid}, headers=hdr)
        assert ok["executed"] is True and ok["status"] == "approved", ok
        pend = [a for a in gw_call("list_approvals", {"status": "approved", "limit": 50})]
        hit = next((a for a in pend if a["id"] == rid), None)
        assert hit and hit["requested_by"] != "", hit
        return f"无令牌被拒 + 请求头令牌放行（{ok['result']['id']}）"

    check("审批令牌闸门（AI 不可自批）", gw_approval_token_enforced)

    def gw_kv_quotes():
        r = gw_call("gateway_call", {"server": "it_ops", "tool": "itops_create_incident",
                                     "arguments": "title='版本 1.10 编号 007';priority=low"})
        assert r["executed"] is True, r
        assert "1.10" in r["result"]["title"] and "007" in r["result"]["title"], r
        return "扁平 kv 串不静默改坏数据（前导零/版本号原样保留）"

    check("kv 入参数值语义保真", gw_kv_quotes)

    if godh:
        def godh_file_root_guard():
            probe = "C:/Windows/win.ini" if os.name == "nt" else "/etc/passwd"
            r = client.call_raw(godh, "data_file_stats", {"path": probe}, headers=GODH_HEADERS)
            assert r.is_error, f"白名单外文件读取竟然放行: {probe}"
            assert "GO_DATAHUB_FILE_ROOTS" in str(r.content), str(r.content)[:200]
            return "文件工具路径围栏生效（越界被拒并提示配置项）"

        def godh_sql_guard():
            dsn = "postgres://u:p@127.0.0.1:1/none"
            r = client.call_raw(godh, "data_db_query_preview",
                                {"dsn": dsn, "sql": "SELECT 1; DROP TABLE t"}, headers=GODH_HEADERS)
            assert r.is_error and "单条" in str(r.content), f"多语句应被拒: {str(r.content)[:160]}"
            r2 = client.call_raw(godh, "data_db_query_preview",
                                 {"dsn": dsn, "sql": "DELETE FROM t"}, headers=GODH_HEADERS)
            assert r2.is_error and "SELECT" in str(r2.content), str(r2.content)[:160]
            r3 = client.call_raw(godh, "data_db_query_preview",
                                 {"dsn": dsn, "sql": "SELECT user, host FROM mysql.user"},
                                 headers=GODH_HEADERS)
            assert r3.is_error and "系统对象" in str(r3.content), str(r3.content)[:160]
            return "任意 SQL 只读护栏（多语句/写语句/账号系统表均拒）"

        def godh_dsn_refs_typed():
            d = client.call(godh, "data_list_dsn_refs", headers=GODH_HEADERS)
            assert isinstance(d.get("refs"), list), d
            if d["refs"]:
                first = d["refs"][0]
                assert isinstance(first, dict) and {"name", "type"} <= set(first), f"refs 形态应为 {{name,type}}: {first!r}"
            assert "://" not in str(d["refs"]), "refs 不得回连接串"
            assert d.get("file_roots"), "应回报文件工具允许目录"
            return f"数据源清单 {len(d['refs'])} 个（含类型）+ file_roots 可见"

        def godh_health_pool_jobs():
            info = client.healthz(godh, "/internal/healthz", headers=GODH_HEADERS)
            assert info["ok"] is True and info.get("version"), info
            assert "pool" in info and "jobs_total" in info, info
            return (f"Go 探活 v{info['version']}（连接池 {info['pool']['pools']} 个，"
                    f"任务 {info['jobs_total']} 条）")

        def godh_job_list_newest():
            ids = [client.call(godh, "data_submit_collect_job",
                               {"source": "synthetic", "params": {"rows": 50, "batch_pause_ms": 0}},
                               headers=GODH_HEADERS)["job_id"] for _ in range(2)]
            deadline = time.time() + 10
            while time.time() < deadline:
                got = [j["id"] for j in client.call(godh, "data_list_jobs", {"limit": 2},
                                                     headers=GODH_HEADERS)["jobs"]]
                if got and got[0] == ids[1]:
                    break
                time.sleep(0.2)
            assert got == list(reversed(ids)), f"list_jobs 应最新在前：期望 {list(reversed(ids))} 实为 {got}"
            return "任务列表最新在前（limit 不再给随机子集）"

        def godh_default_tenant_route():
            r = gw_call_raw("gateway_call", {"server": "go_datahub", "tool": "data_query_dataset",
                                             "arguments": {"dataset_id": "incidents", "tenant_id": "default"}})
            text = str(r.content)
            assert "保留字" not in text, f"tenant_id=default 是入参默认值，不应被路由层拒: {text[:200]}"
            # 本机无真实库：走到"数据源未登记/连不上"才是正确路径（说明兜底路由已解析）
            assert not r.is_error or "itsm_pg" in text or "://" not in text, text[:200]
            return "tenant_id=default 走兜底路由（解析到 dsn_ref 后才谈连库）"

        check("Go 文件工具路径围栏", godh_file_root_guard)
        check("Go 任意 SQL 只读护栏", godh_sql_guard)
        check("Go 数据源清单带类型 + file_roots", godh_dsn_refs_typed)
        check("Go /internal/healthz 概览（版本/池/任务）", godh_health_pool_jobs)
        check("Go data_list_jobs 最新在前", godh_job_list_newest)
        check("Go 数据集 default 租户兜底路由", godh_default_tenant_route)

# ---------------------------------------------------------------------------
# 6) 批量压测（可选）
# ---------------------------------------------------------------------------
def suite_load(gw: str, _itops: str, _common: str, n: int) -> None:
    print(f"== 6. 批量压测（{n} 次并发经网关调用）==", flush=True)
    t0 = time.perf_counter()

    def one(i: int) -> bool:
        r = gw_call( "gateway_call", {
            "server": "common-tools", "tool": "echo", "arguments": {"message": f"load-{i}"},
        })
        return r.get("result") == f"load-{i}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(n, 16)) as pool:
        outcomes = list(pool.map(one, range(n)))

    elapsed = time.perf_counter() - t0
    ok = sum(outcomes)
    _record(f"并发 {n} 次调用全部成功", ok == n, f"{ok}/{n} 成功，耗时 {elapsed:.2f}s")


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="运维工具测试集（真实 HTTP 链路）")
    ap.add_argument("--url", default=client.DEFAULT_URLS["gateway"], help="网关 MCP 地址")
    ap.add_argument("--itops-url", default=client.DEFAULT_URLS["it_ops"], help="it_ops MCP 地址")
    ap.add_argument("--common-url", default=client.DEFAULT_URLS["common-tools"], help="common MCP 地址")
    ap.add_argument("--godh-url", default=os.environ.get("MCP_GODATAHUB_URL", client.DEFAULT_URLS["go_datahub"]),
                    help="go_datahub（Go 重活）MCP 地址（默认 OS 环境变量/配置文件 MCP_GODATAHUB_URL，再默认本机）")
    ap.add_argument("--skip-go", action="store_true", help="跳过 go_datahub 相关用例（未部署 Go server 时用）")
    ap.add_argument("--suite", choices=["all", "check", "smoke", "approve", "guard"], default="all")
    ap.add_argument("--load", type=int, default=0, help="额外跑 N 次并发压测（0=不跑）")
    args = ap.parse_args()

    args_url["gw"] = args.url
    os.environ["MCP_OPS_GATEWAY_URL"] = args.url   # 让 client 知道哪个地址该带网关令牌
    godh = None if args.skip_go else args.godh_url
    print(f"目标：网关 {args.url}\n     it_ops {args.itops_url}\n     common {args.common_url}\n"
          f"     go_datahub {godh or '（跳过）'}\n", flush=True)

    if args.suite in ("all", "check"):
        suite_check(args.url, args.itops_url, args.common_url, godh)
    if args.suite in ("all", "smoke"):
        suite_smoke(args.url, args.itops_url, args.common_url, godh)
    if args.suite in ("all", "approve"):
        suite_approve(args.url, args.itops_url, args.common_url, godh)
    if args.suite in ("all", "guard"):
        suite_guard(args.url, args.itops_url, args.common_url, godh)
    if args.load:
        suite_load(args.url, args.itops_url, args.common_url, args.load)

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed
    print(f"\n{'=' * 52}")
    print(f"结果：{passed} 通过 / {failed} 失败 / 共 {len(_results)} 项")
    if failed:
        print("失败项：")
        for name, ok, detail in _results:
            if not ok:
                print(f"  - {name}: {detail}")
    print("=" * 52)
    print("OPS_TEST_OK" if failed == 0 else "OPS_TEST_FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
