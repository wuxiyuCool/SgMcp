"""内存数据存储：演示用，生产应替换为数据库 / 真实 ITSM 系统 API。"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemoryStore:
    """线程安全的内存仓库，模拟 IT 运维系统的持久化层。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._incidents: dict[str, dict[str, Any]] = {}
        self._changes: dict[str, dict[str, Any]] = {}
        self._assets: dict[str, dict[str, Any]] = {}

    # ---- 工单（Incident / Ticket）----
    def create_incident(self, title: str, priority: str, reporter: str, **extra: Any) -> dict[str, Any]:
        with self._lock:
            rec = {
                "id": f"INC-{uuid.uuid4().hex[:8].upper()}",
                "title": title,
                "priority": priority,
                "reporter": reporter,
                "status": "new",
                "created_at": _now(),
                **extra,
            }
            self._incidents[rec["id"]] = rec
            return dict(rec)

    def list_incidents(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = self._incidents.values()
            if status:
                items = [i for i in items if i["status"] == status]
            return [dict(i) for i in items]

    def update_incident_status(self, incident_id: str, status: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._incidents.get(incident_id)
            if rec is None:
                return None
            rec["status"] = status
            rec["updated_at"] = _now()
            return dict(rec)

    # ---- 变更（Change Request）----
    def create_change(self, title: str, change_type: str, implementer: str, risk: str, **extra: Any) -> dict[str, Any]:
        with self._lock:
            rec = {
                "id": f"CHG-{uuid.uuid4().hex[:8].upper()}",
                "title": title,
                "change_type": change_type,
                "implementer": implementer,
                "risk": risk,
                "status": "draft",
                "created_at": _now(),
                **extra,
            }
            self._changes[rec["id"]] = rec
            return dict(rec)

    def get_change(self, change_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._changes.get(change_id)
            return dict(rec) if rec else None

    # ---- 资产（CMDB Asset）----
    def create_asset(self, name: str, asset_type: str, owner: str, **extra: Any) -> dict[str, Any]:
        with self._lock:
            rec = {
                "id": f"AST-{uuid.uuid4().hex[:8].upper()}",
                "name": name,
                "asset_type": asset_type,
                "owner": owner,
                "status": "in_use",
                "created_at": _now(),
                **extra,
            }
            self._assets[rec["id"]] = rec
            return dict(rec)

    def list_assets(self, asset_type: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = self._assets.values()
            if asset_type:
                items = [a for a in items if a["asset_type"] == asset_type]
            return [dict(a) for a in items]


# 全局单例
store = InMemoryStore()
