"""SQLite 持久化存储：工单/变更/资产跨重启保留。

数据库路径：环境变量 MCP_ITOPS_DB（可写入 config/platform.env），
默认 <工作目录>/data/itops.db。记录整体以 JSON 存 data 列，
status/asset_type 等提升为索引列做过滤——既保留任意扩展字段（**extra），
又能按条件查询。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents(
    id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
CREATE TABLE IF NOT EXISTS changes(
    id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assets(
    id TEXT PRIMARY KEY, asset_type TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_assets_type ON assets(asset_type);
"""


class SqliteStore:
    """线程安全的 SQLite 仓库（SDK 同步工具可能跑在 worker 线程）。"""

    def __init__(self, path: str | None = None) -> None:
        db = path or os.environ.get("MCP_ITOPS_DB") or str(Path("data") / "itops.db")
        Path(db).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---- 通用读写 ----
    def _insert(self, table: str, rec: dict[str, Any], extra_cols: dict[str, str]) -> None:
        cols = ["id", "created_at", "data", *extra_cols]
        vals = [rec["id"], rec["created_at"], json.dumps(rec, ensure_ascii=False), *extra_cols.values()]
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals
            )
            self._conn.commit()

    def _fetch_all(self, table: str, where_col: str | None, value: str | None) -> list[dict[str, Any]]:
        sql = f"SELECT data FROM {table}"
        args: tuple = ()
        if where_col and value:
            sql += f" WHERE {where_col}=?"
            args = (value,)
        sql += " ORDER BY rowid"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [json.loads(r[0]) for r in rows]

    def _fetch_one(self, table: str, rec_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(f"SELECT data FROM {table} WHERE id=?", (rec_id,)).fetchone()
        return json.loads(row[0]) if row else None

    # ---- 工单（Incident / Ticket）----
    def create_incident(self, title: str, priority: str, reporter: str, **extra: Any) -> dict[str, Any]:
        rec = {
            "id": _new_id("INC"),
            "title": title,
            "priority": priority,
            "reporter": reporter,
            "status": "new",
            "created_at": _now(),
            **extra,
        }
        self._insert("incidents", rec, {"status": rec["status"]})
        return dict(rec)

    def list_incidents(self, status: str | None = None) -> list[dict[str, Any]]:
        return self._fetch_all("incidents", "status", status)

    def update_incident_status(self, incident_id: str, status: str) -> dict[str, Any] | None:
        rec = self._fetch_one("incidents", incident_id)
        if rec is None:
            return None
        rec["status"] = status
        rec["updated_at"] = _now()
        with self._lock:
            self._conn.execute(
                "UPDATE incidents SET status=?, data=? WHERE id=?",
                (status, json.dumps(rec, ensure_ascii=False), incident_id),
            )
            self._conn.commit()
        return rec

    # ---- 变更（Change Request）----
    def create_change(self, title: str, change_type: str, implementer: str, risk: str, **extra: Any) -> dict[str, Any]:
        rec = {
            "id": _new_id("CHG"),
            "title": title,
            "change_type": change_type,
            "implementer": implementer,
            "risk": risk,
            "status": "draft",
            "created_at": _now(),
            **extra,
        }
        self._insert("changes", rec, {"status": rec["status"]})
        return dict(rec)

    def get_change(self, change_id: str) -> dict[str, Any] | None:
        return self._fetch_one("changes", change_id)

    # ---- 资产（CMDB Asset）----
    def create_asset(self, name: str, asset_type: str, owner: str, **extra: Any) -> dict[str, Any]:
        rec = {
            "id": _new_id("AST"),
            "name": name,
            "asset_type": asset_type,
            "owner": owner,
            "status": "in_use",
            "created_at": _now(),
            **extra,
        }
        self._insert("assets", rec, {"asset_type": rec["asset_type"]})
        return dict(rec)

    def list_assets(self, asset_type: str | None = None) -> list[dict[str, Any]]:
        return self._fetch_all("assets", "asset_type", asset_type)


# 全局单例
store = SqliteStore()
