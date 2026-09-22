"""运维工具测试集（真实部署链路）。

用 tests/ops/client.py 通过 Streamable HTTP 连真实进程，覆盖日常运维最关心的四类场景：

- check   ：连通性 + 工具清单（三层是否都活着、暴露了哪些工具）
- smoke   ：各层核心工具功能验证（读/写/通用工具）
- approve ：HITL 审批闸门完整流（挂起 → 批准执行 / 驳回不执行）
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

# ---------------------------------------------------------------------------
# 测试框架（极简：不引入 pytest，便于运维直接在目标机跑）
# ---------------------------------------------------------------------------
_results: list[tuple[str, bool, str]] = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f"  — {detail}"
    print(line, flush=True)


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
def suite_check(gw: str, itops: str, common: str) -> None:
    print("== 1. 连通性 & 工具清单 ==", flush=True)

    def gateway_tools():
        names = client.list_tools(gw)
        assert "gateway_call" in names, f"网关缺少统一入口工具，实为 {names}"
        return f"{len(names)} 个工具: {', '.join(sorted(names))}"

    def itops_tools():
        names = client.list_tools(itops)
        for expect in ("create_incident", "list_incidents", "create_change", "register_asset"):
            assert expect in names, f"it_ops 缺少 {expect}"
        return f"{len(names)} 个工具"

    def common_tools():
        names = client.list_tools(common)
        for expect in ("now", "echo", "generate_id", "slugify", "timestamp"):
            assert expect in names, f"common 缺少 {expect}"
        return f"{len(names)} 个工具"

    check("网关可达 + 暴露统一入口", gateway_tools)
    check("it_ops 可达 + 工具齐全", itops_tools)
    check("common-tools 可达 + 工具齐全", common_tools)


# ---------------------------------------------------------------------------
# 2) 冒烟：各层核心功能
# ---------------------------------------------------------------------------
def suite_smoke(gw: str, itops: str, common: str) -> None:
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
        r = client.call(itops, "create_incident", {"title": "运维测试工单", "priority": "high"})
        assert r["id"].startswith("INC-"), r
        return f"{r['id']} status={r['status']}"

    def itops_status_flow():
        created = client.call(itops, "create_incident", {"title": "状态流转测试"})
        iid = created["id"]
        updated = client.call(itops, "update_incident_status", {"incident_id": iid, "status": "in_progress"})
        assert updated["status"] == "in_progress", updated
        listing = client.call(itops, "list_incidents", {"status": "in_progress"})
        ids = [i["id"] for i in listing] if isinstance(listing, list) else []
        assert iid in ids, f"过滤结果未包含 {iid}"
        return f"{iid} new → in_progress 并可按状态过滤"

    def itops_asset():
        r = client.call(itops, "register_asset", {"name": "srv-ops-01", "asset_type": "server", "owner": "ops"})
        assert r["id"].startswith("AST-"), r
        listed = client.call(itops, "list_assets", {"asset_type": "server"})
        assert any(a["id"] == r["id"] for a in listed), "资产未出现在列表中"
        return f"{r['id']} 已登记并可查询"

    check("it_ops 创建工单", itops_create_incident)
    check("it_ops 工单状态流转 + 过滤", itops_status_flow)
    check("it_ops 资产登记 + 查询", itops_asset)

    # 通过网关的统一入口（只读，直接放行）
    def gw_readonly_pass():
        r = client.call(gw, "gateway_call", {
            "server": "common-tools", "tool": "echo", "arguments": {"message": "via-gateway"},
        })
        assert r["approved"] is True and r["executed"] is True, r
        assert r["result"] == "via-gateway", r
        return "只读工具经网关直接放行"

    def gw_itops_readonly():
        r = client.call(gw, "gateway_call", {
            "server": "it_ops", "tool": "list_incidents", "arguments": {},
        })
        assert r["executed"] is True and isinstance(r["result"], list), r
        return f"经网关读取工单 {len(r['result'])} 条"

    check("网关放行只读工具（common）", gw_readonly_pass)
    check("网关路由只读工具（it_ops）", gw_itops_readonly)


# ---------------------------------------------------------------------------
# 3) 审批闸门（HITL）
# ---------------------------------------------------------------------------
def suite_approve(gw: str, _itops: str, _common: str) -> None:
    print("== 3. 审批闸门（HITL）==", flush=True)
    marker = f"运维审批测试-{uuid.uuid4().hex[:6]}"

    # 挂起：高风险写操作不应立即执行
    pending: dict = {}

    def gw_change_pending():
        r = client.call(gw, "gateway_call", {
            "server": "it_ops", "tool": "create_change",
            "arguments": {"title": marker, "risk": "high"},
            "requested_by": "ops-test",
        })
        assert r["approved"] is False and r["executed"] is False, r
        assert r["request_id"].startswith("apr_"), r
        pending["id"] = r["request_id"]
        return f"已挂起 request_id={r['request_id']}"

    check("高风险写操作被挂起（不执行）", gw_change_pending)

    def gw_visible_in_pending():
        items = client.call(gw, "list_pending_approvals", {})
        ids = [i["id"] for i in items]
        assert pending.get("id") in ids, f"待办列表未包含 {pending.get('id')}"
        return f"待审批 {len(items)} 条，含本次请求"

    check("审批单出现在待办列表", gw_visible_in_pending)

    # 批准 → 真正执行到下游
    def gw_approve_executes():
        r = client.call(gw, "approve_request", {"request_id": pending["id"], "comment": "运维测试放行"})
        assert r["executed"] is True, r
        assert r["status"] == "approved", r
        assert "CHG-" in str(r["result"]), r
        pending["change_id"] = r["result"]["id"]
        return f"已执行，产出变更单 {r['result']['id']}"

    check("批准后执行下游并产出变更单", gw_approve_executes)

    def gw_change_readable():
        r = client.call(gw, "gateway_call", {
            "server": "it_ops", "tool": "get_change",
            "arguments": {"change_id": pending["change_id"]},
        })
        assert r["result"]["title"] == marker, r
        return "变更单可经网关查回，标题一致"

    check("审批产物可回查", gw_change_readable)

    # 驳回 → 不执行
    rejected: dict = {}

    def gw_reject_no_execute():
        r = client.call(gw, "gateway_call", {
            "server": "it_ops", "tool": "create_change", "arguments": {"title": f"{marker}-驳回"},
        })
        rid = r["request_id"]
        d = client.call(gw, "reject_request", {"request_id": rid, "comment": "风险过高"})
        assert d["executed"] is False and d["status"] == "rejected", d
        rejected["id"] = rid
        return "驳回后未执行任何下游调用"

    check("驳回后不执行", gw_reject_no_execute)

    def gw_redecide_blocked():
        """已决审批单不能再次决策（状态机保护）。"""
        result = client.call_raw(gw, "approve_request", {"request_id": rejected["id"]})
        assert result.is_error, "已驳回的审批单竟可再次批准"
        return "已决审批单拒绝重复决策"

    check("已决审批单不可重复决策", gw_redecide_blocked)

    # 未注册路由 → 协议错误
    def gw_unknown_route():
        result = client.call_raw(gw, "gateway_call", {"server": "nope", "tool": "x", "arguments": {}})
        assert result.is_error, "未注册路由竟然成功"
        return "未注册路由返回协议错误"

    check("未注册路由报错", gw_unknown_route)


# ---------------------------------------------------------------------------
# 4) 批量压测（可选）
# ---------------------------------------------------------------------------
def suite_load(gw: str, _itops: str, _common: str, n: int) -> None:
    print(f"== 4. 批量压测（{n} 次并发经网关调用）==", flush=True)
    t0 = time.perf_counter()

    def one(i: int) -> bool:
        r = client.call(gw, "gateway_call", {
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
    ap.add_argument("--suite", choices=["all", "check", "smoke", "approve"], default="all")
    ap.add_argument("--load", type=int, default=0, help="额外跑 N 次并发压测（0=不跑）")
    args = ap.parse_args()

    print(f"目标：网关 {args.url}\n     it_ops {args.itops_url}\n     common {args.common_url}\n", flush=True)

    if args.suite in ("all", "check"):
        suite_check(args.url, args.itops_url, args.common_url)
    if args.suite in ("all", "smoke"):
        suite_smoke(args.url, args.itops_url, args.common_url)
    if args.suite in ("all", "approve"):
        suite_approve(args.url, args.itops_url, args.common_url)
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
