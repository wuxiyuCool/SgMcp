"""业务名 → 数据源路由（Python 侧，与 Go 侧 dsrouting 同一套 env 约定）。

对应架构示例：
    db    = route_db("orders", "t1")       # 按租户路由
    table = route_table("orders", "2024-05")  # 按月分表

dataset_id 是业务名（如 orders / incidents），不是表名。

配置约定（env，可写入 config/platform.env；OS 环境变量优先）：
    DATASET_<NAME>_DBTYPE=<pg|mysql|mssql|oracle>   # 库类型（默认 pg）
    DATASET_<NAME>_DSN_<租户大写>=<dsn_ref>          # 租户专属路由（如 t1 → _DSN_T1）
    DATASET_<NAME>_DSN_DEFAULT=<dsn_ref>            # 兜底路由（未命中租户时）

值是 dsn_ref（重活层 DSN_<名称> 注册表的引用名），不存连接串本体。
两端（Go internal/dsrouting）读同一套键，路由结果一致。
"""

from __future__ import annotations

import os
import re
from typing import Any

from mcp_shared.config import load_platform_env

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_TENANT_RE = _IDENT_RE


def _norm(s: str) -> str:
    return s.strip().upper()


def _check_dataset(dataset: str) -> str:
    ds = dataset.strip().lower()
    if not _IDENT_RE.match(ds):
        raise ValueError(f"数据集名非法（小写字母数字下划线）: {dataset!r}")
    return ds


def route_db(dataset: str, tenant: str) -> tuple[str, str]:
    """按业务名 + 租户路由数据源，返回 (db_type, dsn_ref)。

    tenant 传 "default"（各工具默认值）或空串 → 直接取兜底键；其他值先查租户专属键、
    未命中回退兜底。租户名参与拼环境变量键，必须校验字符集——否则调用方能借
    tenant_id 读取任意 `DATASET_*` 键的值（配置注入）。
    """
    ds = _check_dataset(dataset)
    if not tenant or not tenant.strip():
        raise ValueError("tenant_id 不能为空（本域数据集请传 default 走兜底路由）")
    tn = _norm(tenant)
    if tn != "DEFAULT" and not _TENANT_RE.match(tenant.strip().lower()):
        raise ValueError(f"租户名非法（小写字母开头，可含数字/下划线）: {tenant!r}")
    load_platform_env()  # 幂等：把 config/platform.env 的 DATASET_* 注入 os.environ
    base = f"DATASET_{_norm(dataset)}"
    db_type = os.environ.get(f"{base}_DBTYPE") or "pg"
    fallback = f"{base}_DSN_DEFAULT"
    if tn != "DEFAULT":
        specific = f"{base}_DSN_{tn}"
        if os.environ.get(specific):
            return db_type, os.environ[specific].strip().lower()
    if os.environ.get(fallback):
        return db_type, os.environ[fallback].strip().lower()
    raise ValueError(
        f"数据集 {dataset!r} 未配置路由：需要 {base}_DSN_<租户> 或 {fallback}"
        f"（值为 dsn_ref，用 data_list_dsn_refs 查可用数据源、data_list_datasets 查已配置数据集）"
    )


def route_table(dataset: str, period: str) -> str:
    """按业务名 + 周期路由表名（按月分表）：orders + 2024-05 → orders_202405。"""
    ds = _check_dataset(dataset)
    period = period.strip()
    if not _PERIOD_RE.match(period):
        raise ValueError(f"period 格式应为 YYYY-MM（如 2024-05）: {period!r}")
    return f"{ds}_{period.replace('-', '')}"


def table_of(dataset: str) -> str:
    """非周期数据集（如 incidents）的固定表名 = 数据集名。"""
    return _check_dataset(dataset)


def list_datasets() -> list[dict[str, Any]]:
    """枚举已配置的数据集路由（扫描 DATASET_* 键，只回名称与引用名，不回连接串）。

    字段与 Go 侧 dsrouting.DatasetInfo 对齐（dataset/db_type/domain/desc/
    tenants/dsn_tenant_refs/dsn_default），两侧清单一致性由测试断言。
    """
    load_platform_env()
    sets: dict[str, dict[str, Any]] = {}

    def slot(name: str) -> dict[str, Any]:
        return sets.setdefault(name, {"dbtype": "", "default": "", "domain": "",
                                      "desc": "", "tenants": {}})

    for key in os.environ:
        if not key.startswith("DATASET_"):
            continue
        rest = key[len("DATASET_"):]
        val = (os.environ[key] or "").strip()
        if rest.endswith("_DBTYPE"):
            slot(rest[: -len("_DBTYPE")].lower())["dbtype"] = val
        elif rest.endswith("_DSN_DEFAULT"):
            name = rest[: -len("_DSN_DEFAULT")].lower()
            slot(name)["default"] = val.lower()
        elif rest.endswith("_DOMAIN"):
            slot(rest[: -len("_DOMAIN")].lower())["domain"] = val.lower()
        elif rest.endswith("_DESC"):
            slot(rest[: -len("_DESC")].lower())["desc"] = val
        elif "_DSN_" in rest:
            name, tenant = rest.split("_DSN_", 1)
            if tenant and tenant != "DEFAULT":
                slot(name.lower())["tenants"][tenant.lower()] = val.lower()

    out: list[dict[str, Any]] = []
    for name in sorted(sets):
        s = sets[name]
        tenants = sorted(s["tenants"])
        out.append({
            "dataset": name,
            "db_type": s["dbtype"] or "pg",
            "domain": s["domain"],
            "desc": s["desc"],
            "tenants": tenants,
            "dsn_tenant_refs": [s["tenants"][t] for t in tenants],
            "dsn_default": s["default"],
        })
    return out


def dataset_names() -> list[str]:
    """已配置数据集名列表（领域工具生成 Literal 枚举用）。"""
    return [d["dataset"] for d in list_datasets()]


def domain_datasets(domain: str) -> list[str]:
    """某域下的数据集（域归属由 DATASET_<名称>_DOMAIN 决定；未标注域的按 domain 名收）。"""
    want = domain.strip().lower()
    return [d["dataset"] for d in list_datasets()
            if (d["domain"] or want) == want]
