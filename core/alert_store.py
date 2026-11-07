"""形态告警存储（本地 JSON，供 API 与 App 使用）"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT

_LOCK = threading.Lock()
_STORE_PATH = PROJECT_ROOT / "data" / "alerts.json"


@dataclass
class Alert:
    id: str
    symbol: str
    kind: str
    reason: str
    bar_time: str
    created_at: str
    read: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Device:
    token: str
    platform: str
    updated_at: str


def _empty() -> dict[str, Any]:
    return {"alerts": [], "devices": []}


def _load() -> dict[str, Any]:
    if not _STORE_PATH.is_file():
        return _empty()
    try:
        data = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    data.setdefault("alerts", [])
    data.setdefault("devices", [])
    return data


def _save(data: dict[str, Any]) -> None:
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_STORE_PATH)


def add_alert(
    *,
    symbol: str,
    kind: str,
    reason: str,
    bar_time: str,
    extra: dict[str, Any] | None = None,
) -> Alert:
    alert = Alert(
        id=str(uuid.uuid4()),
        symbol=symbol,
        kind=kind,
        reason=reason,
        bar_time=bar_time,
        created_at=datetime.now().isoformat(timespec="seconds"),
        extra=extra or {},
    )
    with _LOCK:
        data = _load()
        data["alerts"].insert(0, asdict(alert))
        data["alerts"] = data["alerts"][:500]
        _save(data)
    return alert


def list_alerts(limit: int = 50, unread_only: bool = False) -> list[dict[str, Any]]:
    with _LOCK:
        data = _load()
    alerts = data["alerts"]
    if unread_only:
        alerts = [a for a in alerts if not a.get("read")]
    return alerts[: max(1, min(limit, 200))]


def mark_read(alert_id: str | None = None, all_alerts: bool = False) -> int:
    with _LOCK:
        data = _load()
        n = 0
        for a in data["alerts"]:
            if all_alerts or a.get("id") == alert_id:
                if not a.get("read"):
                    a["read"] = True
                    n += 1
                if not all_alerts:
                    break
        _save(data)
    return n


def register_device(token: str, platform: str = "android") -> Device:
    token = token.strip()
    if not token:
        raise ValueError("token 不能为空")
    device = Device(
        token=token,
        platform=platform or "android",
        updated_at=datetime.now().isoformat(timespec="seconds"),
    )
    with _LOCK:
        data = _load()
        devices = [d for d in data["devices"] if d.get("token") != token]
        devices.append(asdict(device))
        data["devices"] = devices[-100:]
        _save(data)
    return device


def list_devices() -> list[dict[str, Any]]:
    with _LOCK:
        return list(_load()["devices"])
