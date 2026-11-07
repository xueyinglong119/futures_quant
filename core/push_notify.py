"""向已注册设备发送 Expo Push（App 可安装后收推送）"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from core.alert_store import Alert, list_devices

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def send_alert_push(alert: Alert) -> dict[str, Any]:
    devices = list_devices()
    if not devices:
        return {"sent": 0, "detail": "no_devices"}

    messages = [
        {
            "to": d["token"],
            "title": f"{alert.symbol} {alert.kind}",
            "body": f"{alert.bar_time} {alert.reason}",
            "sound": "default",
            "priority": "high",
            "data": {
                "id": alert.id,
                "symbol": alert.symbol,
                "kind": alert.kind,
            },
        }
        for d in devices
        if d.get("token")
    ]
    if not messages:
        return {"sent": 0, "detail": "no_tokens"}

    req = urllib.request.Request(
        EXPO_PUSH_URL,
        data=json.dumps(messages).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            return {"sent": len(messages), "response": json.loads(body)}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"sent": 0, "error": str(exc)}
