from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models import EconomicIndicator
from app.services.audit import write_audit_log

DEFAULT_INDICATORS = ["treasury_10y", "fed_funds", "inflation_yoy", "unemployment_rate", "vix_proxy"]


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
        row.source = "mock_macro"
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
    if not rows and auto_seed:
        import_fallback_economic_indicators(db)
        rows = list_economic_indicators(db, limit=500)
    latest: dict[str, EconomicIndicator] = {}
    for row in rows:
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
    vix_proxy = value("vix_proxy")
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
    return {
        "source": "mock_macro",
        "indicator_count": len(latest),
        "macro_label": macro_label,
        "rate_regime": rate_regime,
        "inflation_regime": inflation_regime,
        "labor_regime": labor_regime,
        "volatility_regime": volatility_regime,
        "summary": summary,
        "latest": latest_payload,
    }
