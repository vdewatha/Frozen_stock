from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.models import CandidateDecisionJournal, Notification, TradeCandidateSnapshot
from app.services.audit import write_audit_log
from app.services.market_data import get_price_history
from app.services.notifications import create_notification
from app.services.risk import DEFAULT_RISK_RULES
from app.services.risk_settings import get_active_risk_rule


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for item in returns:
        equity *= 1 + item
        peak = max(peak, equity)
        if peak:
            max_dd = min(max_dd, equity / peak - 1)
    return round(max_dd, 6)


def _summary(rows: list[dict], selector_key: str) -> dict:
    selected = [row for row in rows if row.get(selector_key)]
    complete = [row for row in selected if row.get("outcome_return") is not None]
    returns = [float(row["outcome_return"]) for row in complete]
    hits = [item for item in returns if item > 0]
    cumulative = 1.0
    for item in returns:
        cumulative *= 1 + item
    return {
        "selected": len(selected),
        "complete": len(complete),
        "pending": len(selected) - len(complete),
        "hit_rate": round(len(hits) / len(returns), 6) if returns else None,
        "avg_return": round(sum(returns) / len(returns), 6) if returns else None,
        "cumulative_return": round(cumulative - 1, 6) if returns else None,
        "max_drawdown": _max_drawdown(returns) if returns else None,
    }


def _delta(baseline: dict, memory: dict) -> dict:
    return {
        "selected": memory["selected"] - baseline["selected"],
        "complete": memory["complete"] - baseline["complete"],
        "hit_rate": (
            round(float(memory["hit_rate"]) - float(baseline["hit_rate"]), 6)
            if memory["hit_rate"] is not None and baseline["hit_rate"] is not None
            else None
        ),
        "avg_return": (
            round(float(memory["avg_return"]) - float(baseline["avg_return"]), 6)
            if memory["avg_return"] is not None and baseline["avg_return"] is not None
            else None
        ),
        "cumulative_return": (
            round(float(memory["cumulative_return"]) - float(baseline["cumulative_return"]), 6)
            if memory["cumulative_return"] is not None and baseline["cumulative_return"] is not None
            else None
        ),
        "max_drawdown": (
            round(float(memory["max_drawdown"]) - float(baseline["max_drawdown"]), 6)
            if memory["max_drawdown"] is not None and baseline["max_drawdown"] is not None
            else None
        ),
    }


def _replay_gate(rules: dict, baseline: dict, memory: dict) -> dict:
    min_complete = int(rules.get("memory_replay_min_complete_samples", 10) or 0)
    min_avg_delta = float(rules.get("memory_replay_min_avg_return_delta", 0.0) or 0)
    min_hit_delta = float(rules.get("memory_replay_min_hit_rate_delta", 0.0) or 0)
    complete = min(int(baseline.get("complete") or 0), int(memory.get("complete") or 0))
    avg_delta = (
        round(float(memory["avg_return"]) - float(baseline["avg_return"]), 6)
        if memory.get("avg_return") is not None and baseline.get("avg_return") is not None
        else None
    )
    hit_delta = (
        round(float(memory["hit_rate"]) - float(baseline["hit_rate"]), 6)
        if memory.get("hit_rate") is not None and baseline.get("hit_rate") is not None
        else None
    )
    cumulative_delta = (
        round(float(memory["cumulative_return"]) - float(baseline["cumulative_return"]), 6)
        if memory.get("cumulative_return") is not None and baseline.get("cumulative_return") is not None
        else None
    )
    blockers = []
    if complete < min_complete:
        blockers.append(f"Only {complete} completed replay sample(s); requires {min_complete}.")
    if avg_delta is None:
        blockers.append("Average-return delta is pending.")
    elif avg_delta < min_avg_delta:
        blockers.append(f"Average-return delta {avg_delta:.4f} is below required {min_avg_delta:.4f}.")
    if hit_delta is None:
        blockers.append("Hit-rate delta is pending.")
    elif hit_delta < min_hit_delta:
        blockers.append(f"Hit-rate delta {hit_delta:.4f} is below required {min_hit_delta:.4f}.")
    if cumulative_delta is None:
        blockers.append("Cumulative-return delta is pending.")
    elif cumulative_delta < 0:
        blockers.append(f"Cumulative-return delta {cumulative_delta:.4f} is negative.")
    return {
        "status": "open" if not blockers else "closed",
        "allows_memory_increase": not blockers,
        "complete_samples": complete,
        "min_complete_samples": min_complete,
        "avg_return_delta": avg_delta,
        "min_avg_return_delta": min_avg_delta,
        "hit_rate_delta": hit_delta,
        "min_hit_rate_delta": min_hit_delta,
        "cumulative_return_delta": cumulative_delta,
        "blockers": blockers,
    }


def _group_summaries(rows: list[dict], rules: dict, group_key: str, label_key: Optional[str] = None) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        key = str(row.get(group_key) or "unknown")
        grouped.setdefault(key, []).append(row)
    summaries = []
    for key, group_rows in grouped.items():
        baseline = _summary(group_rows, "baseline_selected")
        memory = _summary(group_rows, "memory_selected")
        summaries.append(
            {
                "key": key,
                "label": str(group_rows[0].get(label_key or group_key) or key),
                "row_count": len(group_rows),
                "baseline": baseline,
                "memory_adjusted": memory,
                "delta": _delta(baseline, memory),
                "replay_gate": _replay_gate(rules, baseline, memory),
            }
        )
    return sorted(
        summaries,
        key=lambda item: (
            item["replay_gate"]["allows_memory_increase"] is False,
            -int(item["memory_adjusted"]["complete"] or 0),
            -int(item["memory_adjusted"]["selected"] or 0),
            item["label"],
        ),
    )


def _gate_notification_message(label: str, gate: dict) -> str:
    avg_delta = gate.get("avg_return_delta")
    hit_delta = gate.get("hit_rate_delta")
    cumulative_delta = gate.get("cumulative_return_delta")
    parts = [f"{label} replay gate is open for paper-memory sizing."]
    parts.append(f"{gate.get('complete_samples', 0)} completed replay sample(s).")
    if avg_delta is not None:
        parts.append(f"Average-return delta {float(avg_delta):.2%}.")
    if hit_delta is not None:
        parts.append(f"Hit-rate delta {float(hit_delta):.2%}.")
    if cumulative_delta is not None:
        parts.append(f"Cumulative-return delta {float(cumulative_delta):.2%}.")
    return " ".join(parts)


def _gate_notification_payload(scope: str, key: str, label: str, group: dict) -> dict:
    gate = dict(group.get("replay_gate") or {})
    return {
        "scope": scope,
        "scope_key": key,
        "scope_label": label,
        "row_count": group.get("row_count"),
        "baseline": group.get("baseline"),
        "memory_adjusted": group.get("memory_adjusted"),
        "delta": group.get("delta"),
        "replay_gate": gate,
        "paper_only": True,
    }


def _replay_gate_records(replay: dict) -> list[dict]:
    records = [
        {
            "scope": "global",
            "key": "global",
            "label": "Global replay",
            "group": {
                "row_count": replay.get("evaluated_rows"),
                "baseline": replay.get("baseline"),
                "memory_adjusted": replay.get("memory_adjusted"),
                "delta": replay.get("delta"),
                "replay_gate": replay.get("replay_gate"),
            },
        }
    ]
    group_scopes = [
        ("symbol_strategy", "by_symbol_strategy"),
        ("strategy", "by_strategy"),
        ("symbol", "by_symbol"),
        ("regime", "by_regime"),
    ]
    groups = replay.get("groups") or {}
    for scope, group_key in group_scopes:
        for group in groups.get(group_key) or []:
            records.append(
                {
                    "scope": scope,
                    "key": str(group.get("key")),
                    "label": str(group.get("label") or group.get("key")),
                    "group": group,
                }
            )
    return records


def _matching_gate_notification(open_notifications: list[Notification], scope: str, key: str) -> Optional[Notification]:
    for notification in open_notifications:
        payload = notification.payload or {}
        if payload.get("scope") == scope and payload.get("scope_key") == key:
            return notification
    return None


def sync_memory_replay_gate_notifications(db: Session, replay: Optional[dict] = None) -> dict:
    replay = replay or memory_replay_evaluation(db, notify_gate_opens=False)
    open_notifications = (
        db.query(Notification)
        .filter(Notification.category == "memory_replay")
        .filter(Notification.source == "memory_replay_gate_open")
        .filter(Notification.entity_type == "memory_replay_gate")
        .filter(Notification.status.in_(["open", "acknowledged"]))
        .all()
    )
    checked = 0
    created = 0
    updated = 0
    resolved = 0
    open_gates: list[dict] = []
    current_open_keys: set[tuple[str, str]] = set()
    now = datetime.utcnow()
    for record in _replay_gate_records(replay):
        gate = record["group"].get("replay_gate") or {}
        if not bool(gate.get("allows_memory_increase")):
            continue
        checked += 1
        scope = record["scope"]
        key = record["key"]
        label = record["label"]
        current_open_keys.add((scope, key))
        payload = _gate_notification_payload(scope, key, label, record["group"])
        open_gates.append(payload)
        existing = _matching_gate_notification(open_notifications, scope, key)
        if existing:
            existing.severity = "info"
            existing.title = "Replay gate opened"
            existing.message = _gate_notification_message(label, gate)
            existing.payload = payload
            existing.updated_at = now
            updated += 1
            continue
        create_notification(
            db,
            category="memory_replay",
            severity="info",
            source="memory_replay_gate_open",
            title="Replay gate opened",
            message=_gate_notification_message(label, gate),
            entity_type="memory_replay_gate",
            payload=payload,
        )
        created += 1

    for notification in open_notifications:
        payload = notification.payload or {}
        scope = str(payload.get("scope") or "")
        key = str(payload.get("scope_key") or "")
        if (scope, key) in current_open_keys:
            continue
        notification.status = "resolved"
        if not notification.acknowledged_at:
            notification.acknowledged_at = now
        notification.resolved_at = now
        notification.updated_at = now
        notification.message = "Replay gate is no longer open under the current paper-memory policy."
        resolved += 1

    if created or updated or resolved:
        db.commit()
    return {
        "checked_open_gates": checked,
        "created": created,
        "updated": updated,
        "resolved": resolved,
        "open_gates": jsonable_encoder(open_gates[:20]),
    }


def run_memory_replay_gate_monitor(db: Session, *, source: str = "manual", limit: int = 60, top_k: int = 3) -> dict:
    return {
        "status": "quarantined",
        "source": source,
        "reason": "Legacy memory replay cannot create stock-paper gate state or notifications.",
        "evaluated_rows": 0,
        "complete_rows": 0,
        "pending_rows": 0,
        "approval_alerts": {"checked_open_gates": 0, "created": 0, "updated": 0, "resolved": 0, "open_gates": []},
    }


def _cached_prices(db: Session, cache: dict[str, tuple], symbol: str, lookback: int) -> tuple:
    key = f"{symbol}:{lookback}"
    if key not in cache:
        cache[key] = get_price_history(db, symbol, lookback)
    return cache[key]


def _price_follow_through(db: Session, cache: dict[str, tuple], symbol: str, entry_date: datetime, horizon: int) -> dict:
    prices, source = _cached_prices(db, cache, symbol, 420)
    if prices.empty:
        return {"source": source, "status": "missing", "horizon_days": horizon, "return": None}
    prices = prices.sort_values("date").reset_index(drop=True)
    entry_index: Optional[int] = None
    date_strings = prices["date"].astype(str).tolist()
    entry_date_string = entry_date.date().isoformat()
    for index, item in enumerate(date_strings):
        if item >= entry_date_string:
            entry_index = index
            break
    if entry_index is None:
        entry_index = len(date_strings) - 1
    elif date_strings[entry_index] > entry_date_string and entry_index > 0:
        entry_index -= 1
    target_index = min(entry_index + horizon, len(prices) - 1)
    if target_index <= entry_index:
        return {"source": source, "status": "pending", "horizon_days": horizon, "return": None}
    entry_price = float(prices.iloc[entry_index]["close"])
    exit_price = float(prices.iloc[target_index]["close"])
    return {
        "source": source,
        "status": "complete" if target_index - entry_index >= horizon else "partial",
        "horizon_days": horizon,
        "entry_date": str(prices.iloc[entry_index]["date"]),
        "exit_date": str(prices.iloc[target_index]["date"]),
        "entry_price": round(entry_price, 6),
        "exit_price": round(exit_price, 6),
        "return": round(exit_price / entry_price - 1, 6) if entry_price else None,
    }


def _journal_follow_through(db: Session, cache: dict[str, tuple], row: CandidateDecisionJournal) -> dict:
    evidence = row.evidence_snapshot or {}
    market_data = evidence.get("market_data") or {}
    entry_date = market_data.get("latest_date")
    entry_price = float(market_data.get("latest_close") or 0)
    horizon = int((evidence.get("cached_candidate") or evidence.get("candidate") or {}).get("horizon_days") or 20)
    prices, source = _cached_prices(db, cache, row.symbol, 260)
    if prices.empty or not entry_date or not entry_price:
        return {"source": source, "status": "missing", "horizon_days": horizon, "return": None}

    prices = prices.sort_values("date").reset_index(drop=True)
    entry_index = None
    for index, item in enumerate(prices["date"].astype(str).tolist()):
        if item >= str(entry_date):
            entry_index = index
            break
    if entry_index is None:
        return {"source": source, "status": "missing_entry_date", "horizon_days": horizon, "return": None}

    target_index = min(entry_index + horizon, len(prices) - 1)
    if target_index <= entry_index:
        return {"source": source, "status": "pending", "horizon_days": horizon, "return": None}
    exit_price = float(prices.iloc[target_index]["close"])
    return {
        "source": source,
        "status": "complete" if target_index - entry_index >= horizon else "partial",
        "horizon_days": horizon,
        "entry_date": str(prices.iloc[entry_index]["date"]),
        "exit_date": str(prices.iloc[target_index]["date"]),
        "entry_price": round(entry_price, 6),
        "exit_price": round(exit_price, 6),
        "return": round(exit_price / entry_price - 1, 6) if entry_price else None,
    }


def _candidate_row(
    *,
    source: str,
    source_id: int,
    generated_at,
    candidate: dict,
    baseline_rank: int,
    memory_rank: int,
    top_k: int,
    follow_through: dict,
    base_threshold: float,
    adjusted_threshold: float,
) -> dict:
    base_score = float(candidate.get("base_score", candidate.get("score", 0)) or 0)
    memory_score = float(candidate.get("score") or 0)
    review_adjustment = float(candidate.get("review_threshold_adjustment") or 0)
    outcome_return = follow_through.get("return")
    return {
        "source": source,
        "source_id": source_id,
        "generated_at": generated_at,
        "symbol": candidate.get("symbol"),
        "strategy": candidate.get("strategy"),
        "strategy_name": candidate.get("strategy_name"),
        "symbol_strategy": f"{candidate.get('symbol')}|{candidate.get('strategy')}",
        "symbol_strategy_label": f"{candidate.get('symbol')} | {candidate.get('strategy_name') or candidate.get('strategy')}",
        "market_regime": candidate.get("market_regime", "unknown"),
        "base_score": round(base_score, 4),
        "memory_score": round(memory_score, 4),
        "memory_score_adjustment": round(float(candidate.get("memory_score_adjustment") or 0), 4),
        "review_threshold_adjustment": round(review_adjustment, 4),
        "baseline_rank": baseline_rank,
        "memory_rank": memory_rank,
        "baseline_selected": baseline_rank <= top_k and base_score >= base_threshold,
        "memory_selected": memory_rank <= top_k and memory_score >= adjusted_threshold,
        "base_threshold": round(base_threshold, 4),
        "adjusted_threshold": round(adjusted_threshold, 4),
        "outcome_status": follow_through.get("status"),
        "outcome_return": float(outcome_return) if outcome_return is not None else None,
        "follow_through": follow_through,
    }


def _snapshot_rows(db: Session, *, limit: int, top_k: int, base_threshold: float, adjustment_cap: float, price_cache: dict[str, tuple]) -> list[dict]:
    snapshots = (
        db.query(TradeCandidateSnapshot)
        .order_by(TradeCandidateSnapshot.created_at.desc())
        .limit(min(limit, 100))
        .all()
    )
    rows: list[dict] = []
    for snapshot in snapshots:
        candidates = [
            item
            for item in (snapshot.payload or {}).get("candidates", [])
            if item.get("candidate_status") == "positive_candidate" and item.get("action") == "BUY"
        ]
        baseline_order = sorted(candidates, key=lambda item: float(item.get("base_score", item.get("score", 0)) or 0), reverse=True)
        memory_order = sorted(candidates, key=lambda item: float(item.get("score") or 0), reverse=True)
        baseline_rank = {(item.get("symbol"), item.get("strategy")): index + 1 for index, item in enumerate(baseline_order)}
        memory_rank = {(item.get("symbol"), item.get("strategy")): index + 1 for index, item in enumerate(memory_order)}
        for candidate in candidates:
            key = (candidate.get("symbol"), candidate.get("strategy"))
            adjusted_threshold = max(
                0.0,
                base_threshold + max(-adjustment_cap, min(adjustment_cap, float(candidate.get("review_threshold_adjustment") or 0))),
            )
            follow_through = _price_follow_through(
                db,
                price_cache,
                str(candidate.get("symbol")),
                snapshot.created_at,
                int(candidate.get("horizon_days") or 20),
            )
            rows.append(
                _candidate_row(
                    source="scanner_snapshot",
                    source_id=snapshot.id,
                    generated_at=snapshot.created_at,
                    candidate=candidate,
                    baseline_rank=baseline_rank.get(key, 999),
                    memory_rank=memory_rank.get(key, 999),
                    top_k=top_k,
                    follow_through=follow_through,
                    base_threshold=base_threshold,
                    adjusted_threshold=adjusted_threshold,
                )
            )
    return rows


def _journal_rows(db: Session, *, limit: int, top_k: int, base_threshold: float, adjustment_cap: float, price_cache: dict[str, tuple]) -> list[dict]:
    journals = (
        db.query(CandidateDecisionJournal)
        .order_by(CandidateDecisionJournal.created_at.desc())
        .limit(min(limit, 250))
        .all()
    )
    rows: list[dict] = []
    for journal in journals:
        evidence = journal.evidence_snapshot or {}
        candidate = evidence.get("cached_candidate") or evidence.get("candidate") or {}
        if not candidate:
            continue
        adjusted_threshold = max(
            0.0,
            base_threshold + max(-adjustment_cap, min(adjustment_cap, float(candidate.get("review_threshold_adjustment") or 0))),
        )
        follow_through = _journal_follow_through(db, price_cache, journal)
        if journal.realized_return is not None:
            follow_through = {**follow_through, "status": journal.realized_status or follow_through.get("status"), "return": float(journal.realized_return)}
        rows.append(
            _candidate_row(
                source="decision_journal",
                source_id=journal.id,
                generated_at=journal.created_at,
                candidate=candidate,
                baseline_rank=1,
                memory_rank=1,
                top_k=top_k,
                follow_through=follow_through,
                base_threshold=base_threshold,
                adjusted_threshold=adjusted_threshold,
            )
        )
    return rows


def memory_replay_evaluation(db: Session, *, limit: int = 50, top_k: int = 3, notify_gate_opens: bool = False) -> dict:
    rules = DEFAULT_RISK_RULES | (get_active_risk_rule(db).value or {})
    base_threshold = float(rules.get("candidate_review_score_threshold", 0.70) or 0)
    adjustment_cap = float(rules.get("journal_feedback_review_threshold_cap", 0.03) or 0)
    price_cache: dict[str, tuple] = {}
    rows = _snapshot_rows(db, limit=limit, top_k=top_k, base_threshold=base_threshold, adjustment_cap=adjustment_cap, price_cache=price_cache)
    rows.extend(_journal_rows(db, limit=limit, top_k=top_k, base_threshold=base_threshold, adjustment_cap=adjustment_cap, price_cache=price_cache))
    rows = sorted(rows, key=lambda item: item["generated_at"], reverse=True)
    baseline = _summary(rows, "baseline_selected")
    memory = _summary(rows, "memory_selected")
    replay_gate = _replay_gate(rules, baseline, memory)
    delta = _delta(baseline, memory)
    response = jsonable_encoder(
        {
            "generated_at": datetime.utcnow(),
            "top_k": top_k,
            "evaluated_rows": len(rows),
            "complete_rows": sum(1 for row in rows if row.get("outcome_return") is not None),
            "pending_rows": sum(1 for row in rows if row.get("outcome_return") is None),
            "risk_profile": {
                "candidate_review_score_threshold": base_threshold,
                "journal_feedback_review_threshold_cap": adjustment_cap,
                "memory_replay_min_complete_samples": int(rules.get("memory_replay_min_complete_samples", 10) or 0),
                "memory_replay_min_avg_return_delta": float(rules.get("memory_replay_min_avg_return_delta", 0.0) or 0),
                "memory_replay_min_hit_rate_delta": float(rules.get("memory_replay_min_hit_rate_delta", 0.0) or 0),
            },
            "baseline": baseline,
            "memory_adjusted": memory,
            "delta": delta,
            "replay_gate": replay_gate,
            "groups": {
                "by_symbol_strategy": _group_summaries(rows, rules, "symbol_strategy", "symbol_strategy_label"),
                "by_symbol": _group_summaries(rows, rules, "symbol"),
                "by_strategy": _group_summaries(rows, rules, "strategy", "strategy_name"),
                "by_regime": _group_summaries(rows, rules, "market_regime"),
            },
            "rows": rows[: min(limit, 100)],
        }
    )
    response["approval_alerts"] = {
        "checked_open_gates": 0,
        "created": 0,
        "updated": 0,
        "resolved": 0,
        "open_gates": [],
    }
    return response
