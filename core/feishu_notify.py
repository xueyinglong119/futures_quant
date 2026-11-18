"""严格档告警推送到飞书群机器人（自定义机器人 webhook）。

小米等国产 ROM 的厂商推送通道只对企业开发者开放，个人主体接不了，App 被杀后
自己的长连接也就收不到——所以「不打开 App 也要亮屏」这件事改由飞书 App 自己的
保活来扛：机器人消息到达即系统通知，亮屏、横幅、响铃都归手机系统管。

只推严格档（全市场实测约 0.8 条/交易日），常规档、抢跑、三重仍只进 App 列表。
官方限流 100 次/分钟、5 次/秒，这个量级用不到节流。

配置全走环境变量，缺 URL 就整条静默跳过，不影响告警本身落库：
`FEISHU_WEBHOOK_URL` 机器人地址、`FEISHU_WEBHOOK_SECRET` 机器人开了「签名校验」才填、
`HS_PUBLIC_BASE` 配了就在卡片上加一个「形态图」按钮、`HS_FEISHU_NOTIFY=0` 关掉。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from core.alert_store import Alert

# 配置在发送时现读，不在 import 时读：本模块可能比 config.settings 的 load_dotenv
# 更早被 import，模块级求值会把 .env 里的机器人地址读成空、整条通道静默失效。
_CARD_TEMPLATE = "red"


def _cfg() -> tuple[str, str, str, bool]:
    return (
        os.environ.get("FEISHU_WEBHOOK_URL", "").strip(),
        os.environ.get("FEISHU_WEBHOOK_SECRET", "").strip(),
        os.environ.get("HS_PUBLIC_BASE", "").strip().rstrip("/"),
        os.environ.get("HS_FEISHU_NOTIFY", "1") != "0",
    )


def sign(timestamp: int, secret: str) -> str:
    """飞书官方口径：以 `"{timestamp}\\n{secret}"` 为 **key**、空字节串为消息做 HmacSHA256，再 base64。

    注意 key 和 message 的位置和常规用法相反，写反了服务端会报字段校验失败。
    """
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, msg=b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def build_card(alert: Alert, public_base: str = "") -> dict[str, Any]:
    """把一条严格档告警排成飞书交互卡片。"""
    extra = alert.extra or {}
    base = (public_base or "").strip().rstrip("/")
    name = extra.get("name") or alert.symbol
    duration = extra.get("duration") or ""
    # kind 在严格档已带「·严格」后缀，标题不再重复写一遍
    title = f"{name} {duration} {alert.kind}".strip()

    lines = [f"**{alert.reason}**", f"K线 {alert.bar_time}"]
    detail = extra.get("detail") or {}
    # measured 只有三重通道会带，头肩的形态高度在 swing 里
    measured = detail.get("measured")
    swing = detail.get("swing")
    facts = []
    if isinstance(measured, (int, float)):
        facts.append(f"量度目标 {measured:.0f}")
    if isinstance(swing, (int, float)):
        facts.append(f"形态高度 {swing:.0f}")
    if facts:
        lines.append(" · ".join(facts))

    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
    ]
    if base and alert.id:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "看形态图"},
                        "url": f"{base}/api/alerts/{alert.id}/chart.png",
                    }
                ],
            }
        )
    tail = [t for t in (alert.symbol, duration, str(detail.get("pattern_kind") or "")) if t]
    elements.append(
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": " · ".join(tail)}],
        }
    )
    return {
        "config": {"wide_title_mode": True, "enable_forward": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": _CARD_TEMPLATE,
        },
        "elements": elements,
    }


def send_alert_feishu(alert: Alert) -> dict[str, Any]:
    """发送严格档告警；返回口径与 `send_alert_push` 一致，方便同一行日志打出来。"""
    url, secret, public_base, enabled = _cfg()
    if not enabled:
        return {"sent": 0, "detail": "disabled"}
    if not url:
        return {"sent": 0, "detail": "no_webhook"}

    ts = int(time.time())
    payload: dict[str, Any] = {
        "msg_type": "interactive",
        "card": build_card(alert, public_base),
    }
    if secret:
        payload["timestamp"] = str(ts)
        payload["sign"] = sign(ts, secret)

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"sent": 0, "error": str(exc)}

    code = body.get("code")
    if code == 0:
        return {"sent": 1}
    # 非 0 都当失败：11232 限流、9499 请求体格式错、签名错也是这一路
    return {"sent": 0, "code": code, "msg": body.get("msg")}
