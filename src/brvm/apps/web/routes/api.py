"""JSON endpoints (currently just chart history)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from brvm.services import history, market

router = APIRouter(prefix="/api")


@router.get("/history/{ticker}")
def history_endpoint(ticker: str):
    sec = market.get_security(ticker)
    if sec is None:
        raise HTTPException(status_code=404, detail=f"unknown ticker: {ticker}")
    bars = history.get_history(ticker, sec.country)
    # Lightweight Charts wants oldest -> newest.
    bars_asc = sorted(bars, key=lambda b: b.session_date)
    return JSONResponse(
        [
            {
                "time": b.session_date.isoformat(),
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in bars_asc
        ]
    )
