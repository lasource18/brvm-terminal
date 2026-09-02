"""JSON endpoints (currently just chart history).

`/api/history` is the data behind the Chart tab, which is paid. Hiding
the tab does nothing for this URL — it is a plain GET that returns 25
years of OHLCV as JSON — so the gate lives here too, not only on the
page that draws it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from kodji.apps.web._gating import refuse_if_unpaid
from kodji.services import history, market

router = APIRouter(prefix="/api")


@router.get("/history/{ticker}")
def history_endpoint(request: Request, ticker: str):
    if (refused := refuse_if_unpaid(request, feature="Chart")) is not None:
        return refused
    sec = market.get_security(ticker)
    if sec is None:
        raise HTTPException(status_code=404, detail=f"unknown ticker: {ticker}")
    bars = history.get_history(ticker, sec.country)
    # Lightweight Charts wants oldest -> newest.
    bars_asc = sorted(bars, key=lambda b: b.session_date)
    return JSONResponse(
        {
            "kind": sec.kind,
            "bars": [
                {
                    "time": b.session_date.isoformat(),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in bars_asc
            ],
        }
    )
