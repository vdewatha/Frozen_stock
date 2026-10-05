"""Fixed prospective evaluation slots, without selecting by outcome or action."""
from datetime import timedelta

from app.services.intraday_data import _aware_utc
from app.services.online_return_challenger import summarize

VERSION = "iex-return-ten-minute-cohort-v1"


def plan(issued_at, target_at):
    issued, target = _aware_utc(issued_at), _aware_utc(target_at)
    slot = issued.replace(second=0, microsecond=0)
    included = (slot.minute % 10 == 0 and issued < target
                and target + timedelta(minutes=1) <= slot + timedelta(minutes=10))
    return {"version": VERSION, "included": included,
            "slot_at": slot.isoformat() if included else None}


def report(rows, *, symbol, now):
    selected, slots = [], set()
    invalid = excluded = 0
    for row in sorted(rows, key=lambda r: (_aware_utc(r.issued_at), r.id)):
        if not isinstance(row.prediction, dict):
            invalid += 1
            continue
        declaration = row.prediction.get("return_evaluation_cohort")
        if declaration is None:
            continue
        expected = plan(row.issued_at, row.target_at)
        if (row.symbol != symbol or row.version != "iex-sgd-v1"
                or declaration != expected or "return_challenger" not in row.prediction):
            invalid += 1
            continue
        if not declaration["included"]:
            excluded += 1
            continue
        # Keep the first declared forecast even if it later expires or is missing.
        slot = declaration["slot_at"]
        if slot in slots:
            invalid += 1
            continue
        slots.add(slot)
        selected.append(row)
    scored = [r for r in selected if r.status == "scored"]
    result = summarize(scored, symbol=symbol, now=now)
    unknown = sum(r.status not in {"scored", "pending", "expired"} for r in selected)
    return {"version": VERSION, "selection": "predeclared_utc_ten_minute_slots",
            "scope": "provided_forecast_window_not_all_market_opportunities",
            "selected_forecasts": len(selected), "excluded_off_slot": excluded,
            "pending": sum(r.status == "pending" for r in selected),
            "expired": sum(r.status == "expired" for r in selected),
            "invalid_declarations": invalid + unknown,
            "paired_observations": result["paired_observations"],
            "unavailable_observations": result["unavailable_observations"],
            "invalid_observations": result["invalid_observations"],
            "strategies": result["strategies"], "cost_bps_per_side": result["cost_bps_per_side"],
            "overlapping_windows": False, "independence_proven": False,
            "profitability_proven": False, "execution_eligible": False}
