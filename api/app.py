"""HTTP API 服务"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.alert_store import add_alert, list_alerts, mark_read, register_device
from core.market_data import DEFAULT_KLINE_SECONDS, get_today_klines
from core.push_notify import send_alert_push

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

app = FastAPI(
    title="Futures Quant API",
    description="期货量化系统 HTTP 接口",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if WEB_DIR.is_dir():
    app.mount("/web", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


class CreateAlertBody(BaseModel):
    symbol: str
    kind: str
    reason: str
    bar_time: str
    extra: dict[str, Any] = Field(default_factory=dict)
    push: bool = True


class RegisterDeviceBody(BaseModel):
    token: str
    platform: str = "android"


@app.get("/")
def index() -> FileResponse:
    page = WEB_DIR / "fu_kline.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="页面不存在")
    return FileResponse(page)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/klines")
def api_klines(
    symbol: str = Query(..., description="合约号，如 MA609、CZCE.MA609"),
    date_str: str | None = Query(
        None,
        alias="date",
        description="日期 YYYY-MM-DD，默认今天",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
    minutes: int = Query(
        DEFAULT_KLINE_SECONDS // 60,
        ge=1,
        le=1440,
        description="K 线周期（分钟），默认 30",
    ),
    time_label: Literal["start", "end"] = Query(
        "end",
        description="datetime 标注：end=国内软件常用时刻(默认)，start=第一根交易分钟",
    ),
    session_aligned: bool = Query(
        True,
        description="30分钟时按国内小节休市规则从1分钟K线合成",
    ),
) -> JSONResponse:
    """
    获取指定合约某自然日的 K 线数据。

    示例: GET /api/klines?symbol=MA609&date=2026-06-15&minutes=30
    """
    trade_date: date | None = None
    if date_str:
        try:
            trade_date = date.fromisoformat(date_str)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="date 格式应为 YYYY-MM-DD") from exc

    try:
        result = get_today_klines(
            symbol,
            trade_date=trade_date,
            duration_seconds=minutes * 60,
            time_label=time_label,
            session_aligned=session_aligned,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取 K 线失败: {exc}") from exc

    return JSONResponse(content=result)


@app.get("/api/alerts")
def api_list_alerts(
    limit: int = Query(50, ge=1, le=200),
    unread_only: bool = Query(False),
) -> dict[str, Any]:
    items = list_alerts(limit=limit, unread_only=unread_only)
    return {"count": len(items), "alerts": items}


@app.post("/api/alerts")
def api_create_alert(body: CreateAlertBody) -> dict[str, Any]:
    alert = add_alert(
        symbol=body.symbol.strip(),
        kind=body.kind.strip(),
        reason=body.reason.strip(),
        bar_time=body.bar_time.strip(),
        extra=body.extra,
    )
    push_result: dict[str, Any] = {"sent": 0}
    if body.push:
        push_result = send_alert_push(alert)
    return {"alert": alert.__dict__, "push": push_result}


@app.post("/api/alerts/read")
def api_mark_alerts_read(
    alert_id: str | None = Query(None),
    all_alerts: bool = Query(False, alias="all"),
) -> dict[str, Any]:
    if not all_alerts and not alert_id:
        raise HTTPException(status_code=400, detail="请提供 alert_id 或 all=true")
    n = mark_read(alert_id=alert_id, all_alerts=all_alerts)
    return {"updated": n}


@app.post("/api/devices/register")
def api_register_device(body: RegisterDeviceBody) -> dict[str, Any]:
    try:
        device = register_device(body.token, body.platform)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"device": device.__dict__}
