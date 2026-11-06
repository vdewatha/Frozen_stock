from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import logging
from typing import Callable, Optional

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import EconomicIndicator
from app.services.audit import write_audit_log

logger = logging.getLogger(__name__)

DEFAULT_INDICATORS = ["treasury_10y", "fed_funds", "inflation_yoy", "unemployment_rate", "vix_proxy"]
MOCK_SOURCE = "mock_macro"
FRED_SOURCE = "fred"
FRED_URL = "https://api.stlouisfed.org/fred"
# indicator -> (FRED series, unit). CPI is an index; inflation_yoy is derived.
FRED_SERIES = {
    "treasury_10y": ("DGS10", "percent"),
    "fed_funds": ("DFF", "percent"),
    "unemployment_rate": ("UNRATE", "percent"),
    "vix_proxy": ("VIXCLS", "index"),
    "cpi_index": ("CPIAUCSL", "index"),
}
FRED_LOOKBACK_DAYS = 430  # 13+ months, so the latest CPI has a year-ago match


def fred_configured() -> bool:
    return bool(settings.fred_api_key.get_secret_value().strip())


def fred_get(path: str, params: dict, *, client: Optional[httpx.Client] = None) -> dict:
    """One FRED API call. The key goes in the query string, as FRED requires."""
    query = {**params, "api_key": settings.fred_api_key.get_secret_value().strip(), "file_type": "json"}
    owned = client is None
    client = client or httpx.Client(timeout=20)
    try:
        response = client.get(f"{FRED_URL}/{path}", params=query)
        response.raise_for_status()
        return response.json()
    finally:
        if owned:
            client.close()


def _fred_observations(series_id: str, start: date, fetch: Callable) -> list[tuple[date, float]]:
    payload = fetch("series/observations", {"series_id": series_id, "observation_start": start.isoformat()})
    rows = []
    for item in payload.get("observations") or []:
        try:  # FRED writes "." for a missing day
            rows.append((date.fromisoformat(item["date"]), float(item["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def _year_over_year(index_rows: list[tuple[date, float]]) -> list[tuple[date, float]]:
    by_month = {(day.year, day.month): value for day, value in index_rows}
    out = []
    for day, value in index_rows:
        base = by_month.get((day.year - 1, day.month))
        if base:
            out.append((day, (value / base - 1) * 100))
    return out


def import_fred_economic_indicators(db: Session, *, today: Optional[date] = None,
                                    fetch: Optional[Callable] = None) -> dict:
    """Real rates, inflation, unemployment and VIX from FRED (St. Louis Fed)."""
    today = today or date.today()
    fetch = fetch or fred_get
    start = today - timedelta(days=FRED_LOOKBACK_DAYS)
    series: dict[str, tuple[list[tuple[date, float]], str, str]] = {}
    errors = {}
    for name, (series_id, unit) in FRED_SERIES.items():
        try:
            series[name] = (_fred_observations(series_id, start, fetch), unit, series_id)
        except Exception as exc:  # one series failing must not drop the others
            errors[name] = exc.__class__.__name__
            logger.info("FRED %s unavailable: %s", series_id, exc.__class__.__name__)
    if "cpi_index" in series:
        cpi_rows, _, cpi_id = series["cpi_index"]
        series["inflation_yoy"] = (_year_over_year(cpi_rows), "percent", f"{cpi_id} year over year")

    names = list(series)
    earliest = min((day for rows, _, _ in series.values() for day, _ in rows), default=start)
    existing = {
        (row.indicator_name, row.observation_date): row
        for row in db.query(EconomicIndicator).filter(
            EconomicIndicator.indicator_name.in_(names), EconomicIndicator.observation_date >= earliest)
    }
    imported = updated = 0
    for name, (rows, unit, series_id) in series.items():
        for day, value in rows:
            row = existing.get((name, day))
            if row is None:
                row = EconomicIndicator(indicator_name=name, observation_date=day)
                db.add(row)
                imported += 1
            elif row.source == FRED_SOURCE and row.value == _decimal(value):
                continue
            else:
                updated += 1
            row.value = _decimal(value)
            row.unit = unit
            row.source = FRED_SOURCE
            row.raw_payload = {"series_id": series_id}
    db.flush()
    status = "complete" if not errors else ("partial" if series else "failed")
    write_audit_log(
        db, event_type="economic_data", entity_type="economic_indicator", action="import_fred",
        status=status, message=f"FRED: {imported} new and {updated} updated observations"
                               + (f"; failed: {', '.join(sorted(errors))}" if errors else "."),
        payload={"source": FRED_SOURCE, "rows_imported": imported, "rows_updated": updated,
                 "series": {name: len(rows) for name, (rows, _, _) in series.items()}, "errors": errors},
    )
    db.commit()
    return {"source": FRED_SOURCE, "status": status, "rows_imported": imported, "rows_updated": updated,
            "errors": errors}


def import_economic_indicators(db: Session) -> dict:
    """FRED when a key is configured; the labelled placeholder rows otherwise."""
    if fred_configured():
        return import_fred_economic_indicators(db)
    return import_fallback_economic_indicators(db)


def _decimal(value: float) -> Decimal:
    return Decimal(str(round(float(value), 6)))


def _fallback_macro_rows(as_of: Optional[date] = None) -> list[dict]:
    as_of = as_of or date.today()
    monthly_dates = [as_of - timedelta(days=30 * offset) for offset in reversed(range(6))]
    rows: list[dict] = []
    for index, observation_date in enumerate(monthly_dates):
        rows.extend(
            [
                {
                    "indicator_name": "treasury_10y",
                    "observation_date": observation_date,
                    "value": 4.05 + index * 0.03,
                    "unit": "percent",
                },
                {
                    "indicator_name": "fed_funds",
                    "observation_date": observation_date,
                    "value": 4.75,
                    "unit": "percent",
                },
                {
                    "indicator_name": "inflation_yoy",
                    "observation_date": observation_date,
                    "value": 3.25 - index * 0.04,
                    "unit": "percent",
                },
                {
                    "indicator_name": "unemployment_rate",
                    "observation_date": observation_date,
                    "value": 4.0 + index * 0.02,
                    "unit": "percent",
                },
                {
                    "indicator_name": "vix_proxy",
                    "observation_date": observation_date,
                    "value": 16.5 + index * 0.7,
                    "unit": "index",
                },
            ]
        )
    return rows


def import_fallback_economic_indicators(db: Session) -> dict:
    rows = _fallback_macro_rows()
    imported = 0
    touched_ids: list[int] = []
    for payload in rows:
        row = (
            db.query(EconomicIndicator)
            .filter(
                EconomicIndicator.indicator_name == payload["indicator_name"],
                EconomicIndicator.observation_date == payload["observation_date"],
            )
            .one_or_none()
        )
        if not row:
            row = EconomicIndicator(
                indicator_name=payload["indicator_name"],
                observation_date=payload["observation_date"],
            )
            db.add(row)
            imported += 1
        row.value = _decimal(payload["value"])
        row.unit = payload["unit"]
        row.source = MOCK_SOURCE
        row.raw_payload = {"fallback": True}
        db.flush()
        touched_ids.append(row.id)

    write_audit_log(
        db,
        event_type="economic_data",
        entity_type="economic_indicator",
        entity_id=touched_ids[0] if touched_ids else None,
        action="import_fallback_macro",
        status="complete",
        message=f"Stored {len(touched_ids)} fallback economic indicator observations.",
        payload={"source": "mock_macro", "indicator_ids": touched_ids},
    )
    db.commit()
    return {"source": "mock_macro", "rows_imported": imported, "indicator_ids": touched_ids}


def list_economic_indicators(db: Session, indicator_name: Optional[str] = None, limit: int = 100) -> list[EconomicIndicator]:
    query = db.query(EconomicIndicator)
    if indicator_name:
        query = query.filter(EconomicIndicator.indicator_name == indicator_name)
    return query.order_by(EconomicIndicator.observation_date.desc(), EconomicIndicator.indicator_name).limit(min(limit, 500)).all()


def latest_indicator_map(db: Session, auto_seed: bool = True) -> dict:
    rows = list_economic_indicators(db, limit=500)
    if not rows and auto_seed and not fred_configured():
        import_fallback_economic_indicators(db)
        rows = list_economic_indicators(db, limit=500)
    # A real observation always beats a placeholder: the mock rows are dated
    # today, so newest-first alone would keep showing them after FRED lands.
    real_names = {row.indicator_name for row in rows if row.source != MOCK_SOURCE}
    latest: dict[str, EconomicIndicator] = {}
    for row in rows:
        if row.indicator_name in real_names and row.source == MOCK_SOURCE:
            continue
        if row.indicator_name not in latest:
            latest[row.indicator_name] = row
    return latest


def summarize_macro_context(db: Session, auto_seed: bool = True) -> dict:
    latest = latest_indicator_map(db, auto_seed=auto_seed)
    if not latest:
        return {
            "source": "none",
            "indicator_count": 0,
            "macro_label": "unknown",
            "rate_regime": "unknown",
            "inflation_regime": "unknown",
            "labor_regime": "unknown",
            "volatility_regime": "unknown",
            "summary": "No macro context is available.",
            "latest": {},
        }

    def value(name: str) -> float:
        return float(latest[name].value or 0) if name in latest else 0.0

    treasury_10y = value("treasury_10y")
    inflation = value("inflation_yoy")
    unemployment = value("unemployment_rate")
    vix_proxy = value("vix") if "vix" in latest else value("vix_proxy")
    rate_regime = "restrictive" if treasury_10y >= 4.25 else "neutral" if treasury_10y >= 3.5 else "easy"
    inflation_regime = "sticky" if inflation >= 3.0 else "cooling"
    labor_regime = "softening" if unemployment >= 4.3 else "steady"
    volatility_regime = "elevated" if vix_proxy >= 20 else "normal"

    if rate_regime == "restrictive" and inflation_regime == "sticky":
        macro_label = "tight_policy"
    elif labor_regime == "softening" and volatility_regime == "elevated":
        macro_label = "growth_stress"
    elif inflation_regime == "cooling" and volatility_regime == "normal":
        macro_label = "goldilocks"
    else:
        macro_label = "mixed_macro"

    latest_payload = {
        name: {
            "value": round(float(row.value or 0), 4),
            "unit": row.unit,
            "observation_date": row.observation_date.isoformat(),
            "source": row.source,
        }
        for name, row in latest.items()
    }
    summary = (
        f"Macro backdrop is {macro_label}: 10Y {treasury_10y:.2f}%, "
        f"inflation {inflation:.2f}%, unemployment {unemployment:.2f}%, VIX proxy {vix_proxy:.1f}."
    )
    sources = sorted({row.source or "unknown" for row in latest.values()})
    return {
        "source": sources[0] if len(sources) == 1 else "mixed",
        "indicator_count": len(latest),
        "macro_label": macro_label,
        "rate_regime": rate_regime,
        "inflation_regime": inflation_regime,
        "labor_regime": labor_regime,
        "volatility_regime": volatility_regime,
        "summary": summary,
        "latest": latest_payload,
    }
