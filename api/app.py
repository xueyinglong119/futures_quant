"""HTTP API 服务"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.alert_store import add_alert, clear_alerts, list_alerts, mark_read, register_device
from core.hs_chart import render_hs_chart
from utils.symbols import get_display_name
from core.indicators import (
    bars_from_records,
    compute_overlay_indicators,
    latest_indicator_snapshot,
)
from core.kline_store import LABEL_TO_DURATION, connect, ensure_table, load_bars
from core.paper_trade import account_snapshot, alert_execution, manual_close, trading_board
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


@app.get("/api/indicators")
def api_indicators(
    symbol: str = Query(..., description="主连/合约，如 KQ.m@DCE.eg"),
    duration: str = Query(
        "1d",
        description="周期: 1m/3m/5m/10m/15m/30m/1h/2h/3h/4h/1d/1w/1mon",
    ),
    limit: int = Query(
        0,
        ge=0,
        le=5000,
        description="返回最近 N 根明细，0 表示只返回最新快照",
    ),
) -> JSONResponse:
    """
    基于已入库 K 线计算 MA / BOLL / EXPMA / 神奇九转。

    示例: GET /api/indicators?symbol=KQ.m@DCE.eg&duration=1d&limit=20
    """
    label = duration.strip().lower()
    if label not in LABEL_TO_DURATION:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的周期 {duration}，可选: {', '.join(LABEL_TO_DURATION)}",
        )

    conn = connect()
    ensure_table(conn)
    try:
        records = load_bars(conn, symbol.strip(), LABEL_TO_DURATION[label])
    finally:
        conn.close()

    if not records:
        raise HTTPException(status_code=404, detail="库中无该品种/周期数据")

    df = bars_from_records(records)
    enriched = compute_overlay_indicators(df)
    snap = latest_indicator_snapshot(enriched)

    payload: dict[str, Any] = {
        "symbol": symbol.strip(),
        "duration": label,
        "bars": len(enriched),
        "latest": snap,
    }
    if limit > 0:
        tail = enriched.tail(limit).copy()
        # datetime 转字符串，便于 JSON
        if "datetime_end" in tail.columns:
            tail["datetime_end"] = tail["datetime_end"].astype(str)
        for col in ("td_sell_nine", "td_buy_nine"):
            if col in tail.columns:
                tail[col] = tail[col].astype(bool)
        payload["series"] = json_safe_records(tail)
    return JSONResponse(content=payload)


def json_safe_records(df) -> list[dict[str, Any]]:
    import math

    import pandas as pd

    rows: list[dict[str, Any]] = []
    for rec in df.to_dict(orient="records"):
        clean: dict[str, Any] = {}
        for k, v in rec.items():
            if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                clean[k] = None
            elif isinstance(v, (pd.Timestamp,)):
                clean[k] = str(v)
            else:
                clean[k] = v
        rows.append(clean)
    return rows


@app.get("/board")
def board_page() -> FileResponse:
    page = WEB_DIR / "paper.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="看板不存在")
    return FileResponse(page)


@app.get("/backtest")
def backtest_page() -> FileResponse:
    page = ROOT / "reports" / "backtest" / "gallery_single.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="回测尚未生成（reports/backtest/gallery_single.html 不存在）")
    return FileResponse(page, media_type="text/html")


@app.get("/backtest/killed")
def backtest_killed_page() -> FileResponse:
    page = ROOT / "reports" / "backtest" / "gallery_killed.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="被滤信号画廊尚未生成（reports/backtest/gallery_killed.html 不存在）")
    return FileResponse(page, media_type="text/html")


@app.get("/alerts")
def alerts_page() -> FileResponse:
    page = WEB_DIR / "alerts.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="告警页不存在")
    return FileResponse(page)


@app.get("/api/paper")
def api_paper(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    """模拟账户：本金、持仓、每笔开仓手续费和盈亏。"""
    conn = connect()
    try:
        return account_snapshot(conn, limit=limit)
    finally:
        conn.close()


@app.get("/api/paper/board")
def api_paper_board(
    day: str | None = Query(None, alias="date", description="北京时间日期 YYYY-MM-DD，默认今天"),
) -> dict[str, Any]:
    """按北京时间的某一天汇总模拟交易，并给出可切换的日期。"""
    selected: date | None = None
    if day:
        try:
            selected = date.fromisoformat(day.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD") from exc
    conn = connect()
    try:
        return trading_board(conn, selected)
    finally:
        conn.close()


@app.post("/api/paper/close")
def api_paper_close(trade_id: int = Query(..., ge=1, description="持仓单号")) -> dict[str, Any]:
    """手动平掉一笔模拟持仓，按最新入库价成交。"""
    conn = connect()
    try:
        try:
            result = manual_close(conn, trade_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}
    finally:
        conn.close()


@app.get("/api/alerts")
def api_list_alerts(
    limit: int = Query(50, ge=1, le=200),
    unread_only: bool = Query(False),
) -> dict[str, Any]:
    items = list_alerts(limit=limit, unread_only=unread_only)
    for item in items:
        symbol = str(item.get("symbol") or "")
        extra = item.get("extra") or {}
        if not isinstance(extra, dict):
            extra = {}
        name = str(extra.get("name") or (get_display_name(symbol) if symbol else ""))
        duration = str(extra.get("duration") or "")
        extra = {
            **extra,
            "name": name,
            "code": symbol,
            "display": f"{name}({symbol})" if name else symbol,
        }
        item["extra"] = extra
        # 旧版 App 只展示 symbol · kind，这里直接返回可读标题
        title = name or symbol
        if duration:
            title = f"{title} · {duration}"
        item["symbol"] = title
    return {"count": len(items), "alerts": items}


@app.get("/api/alerts/{alert_id}/execution")
def api_alert_execution(alert_id: str) -> dict[str, Any]:
    """告警对应的模拟执行情况：开仓价/手数/止盈止损/浮动或净盈亏。"""
    conn = connect()
    try:
        return alert_execution(conn, alert_id.strip())
    finally:
        conn.close()


@app.get("/api/alerts/{alert_id}/chart.png")
def api_alert_chart(alert_id: str) -> Response:
    """告警的头肩形态 K 线图：蜡烛 + 左/头/右标记 + 颈线。"""
    alert = next((a for a in list_alerts(limit=1000) if a.get("id") == alert_id.strip()), None)
    if alert is None:
        raise HTTPException(404, "告警不存在")
    extra = alert.get("extra") or {}
    detail = extra.get("detail") or {}
    duration_sec = extra.get("duration_sec")
    if not detail or not duration_sec:
        raise HTTPException(404, "该告警没有形态关键点")
    conn = connect()
    try:
        png = render_hs_chart(alert["symbol"], int(duration_sec), alert["bar_time"], detail, conn)
    finally:
        conn.close()
    if not png:
        raise HTTPException(404, "没有可画的 K 线")
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.post("/api/alerts/clear")
def api_clear_alerts() -> dict[str, Any]:
    """清空历史告警（启动误刷 / 测试数据）。"""
    n = clear_alerts()
    return {"cleared": n}


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
