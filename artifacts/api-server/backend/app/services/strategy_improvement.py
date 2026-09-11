from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.models import Strategy, StrategyExperiment, StrategyMemory
from app.services.governance import (
    MAX_MEMORY_DRAWDOWN,
    MIN_CONFIDENCE_SCORE,
    MIN_PAPER_TRADES_FOR_ACTIVE,
    MIN_PROFIT_FACTOR,
    MIN_WIN_RATE,
    RETIRE_DRAWDOWN,
)
from app.services.learning import propose_parameter_experiments


IMPROVEMENT_STATUSES = {"paused", "retired"}


def _latest_memory_for_strategy(db: Session, strategy_id: int) -> Optional[StrategyMemory]:
    return (
        db.query(StrategyMemory)
        .filter(StrategyMemory.strategy_id == strategy_id)
        .order_by(StrategyMemory.last_updated.desc())
        .first()
    )


def _latest_experiment_for_strategy(db: Session, strategy_id: int) -> Optional[StrategyExperiment]:
    return (
        db.query(StrategyExperiment)
        .filter(StrategyExperiment.strategy_id == strategy_id)
        .order_by(StrategyExperiment.created_at.desc(), StrategyExperiment.id.desc())
        .first()
    )


def _memory_snapshot(memory: Optional[StrategyMemory]) -> dict:
    if not memory:
        return {
            "sample_size": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "avg_drawdown": 0.0,
            "confidence_score": 0.0,
            "symbol": "SPY",
            "market_regime": "unclassified",
            "notes": None,
        }
    return {
        "sample_size": int(memory.sample_size or 0),
        "win_rate": float(memory.win_rate or 0),
        "profit_factor": float(memory.profit_factor or 0),
        "avg_drawdown": float(memory.avg_drawdown or 0),
        "confidence_score": float(memory.confidence_score or 0),
        "symbol": memory.symbol or "SPY",
        "market_regime": memory.market_regime or "unclassified",
        "notes": memory.notes,
    }


def _improvement_reasons(status: str, memory: dict) -> list[str]:
    reasons: list[str] = []
    if status == "retired":
        reasons.append("Strategy is retired and must prove a new paper-only thesis before reactivation.")
    if status == "paused":
        reasons.append("Strategy is paused and needs improved paper evidence before review.")
    if memory["sample_size"] < MIN_PAPER_TRADES_FOR_ACTIVE:
        reasons.append(f"Only {memory['sample_size']} closed paper sample(s); active review needs at least {MIN_PAPER_TRADES_FOR_ACTIVE}.")
    if memory["win_rate"] < MIN_WIN_RATE:
        reasons.append(f"Paper win rate {memory['win_rate']:.2f} is below the {MIN_WIN_RATE:.2f} minimum.")
    if memory["profit_factor"] < MIN_PROFIT_FACTOR:
        reasons.append(f"Profit factor {memory['profit_factor']:.2f} is below the {MIN_PROFIT_FACTOR:.2f} minimum.")
    if memory["confidence_score"] < MIN_CONFIDENCE_SCORE:
        reasons.append(f"Confidence {memory['confidence_score']:.2f} is below the {MIN_CONFIDENCE_SCORE:.2f} promotion floor.")
    if memory["avg_drawdown"] <= MAX_MEMORY_DRAWDOWN:
        reasons.append(f"Drawdown {memory['avg_drawdown']:.2f} breaches the {MAX_MEMORY_DRAWDOWN:.2f} pause limit.")
    if memory["avg_drawdown"] <= RETIRE_DRAWDOWN:
        reasons.append(f"Drawdown {memory['avg_drawdown']:.2f} is at or beyond the {RETIRE_DRAWDOWN:.2f} retirement limit.")
    return reasons or ["Paused or retired lifecycle status requires a fresh paper-only experiment plan."]


def _paper_gates(memory: dict) -> list[dict]:
    return [
        {
            "name": "Backtest improvement",
            "status": "required",
            "requirement": "Candidate parameters must beat the current score by at least 0.03 without materially worse drawdown.",
        },
        {
            "name": "Trade count",
            "status": "required",
            "requirement": "Candidate backtest must keep enough trades to avoid a thin-sample promotion.",
        },
        {
            "name": "Paper sample",
            "status": "blocked" if memory["sample_size"] < MIN_PAPER_TRADES_FOR_ACTIVE else "ready",
            "requirement": f"At least {MIN_PAPER_TRADES_FOR_ACTIVE} closed paper trades before active-strategy review.",
        },
        {
            "name": "Reactivation review",
            "status": "required",
            "requirement": "A human review must approve reactivation; OpenAI and experiments cannot execute trades.",
        },
    ]


def strategy_improvement_queue(db: Session) -> dict:
    strategies = (
        db.query(Strategy)
        .filter(Strategy.current_status.in_(IMPROVEMENT_STATUSES))
        .order_by(Strategy.current_status.desc(), Strategy.name)
        .all()
    )
    rows = []
    for strategy in strategies:
        memory = _memory_snapshot(_latest_memory_for_strategy(db, strategy.id))
        latest_experiment = _latest_experiment_for_strategy(db, strategy.id)
        proposals = propose_parameter_experiments(strategy.strategy_type, strategy.parameters or {})[:3]
        severity = "critical" if strategy.current_status == "retired" else "warning"
        rows.append(
            {
                "strategy_id": strategy.id,
                "strategy_name": strategy.name,
                "strategy_type": strategy.strategy_type,
                "current_status": strategy.current_status,
                "severity": severity,
                "symbol": memory["symbol"],
                "market_regime": memory["market_regime"],
                "memory": memory,
                "improvement_reasons": _improvement_reasons(strategy.current_status, memory),
                "proposed_experiments": proposals,
                "paper_gates": _paper_gates(memory),
                "latest_experiment": {
                    "id": latest_experiment.id,
                    "decision": latest_experiment.decision,
                    "experiment_name": latest_experiment.experiment_name,
                    "created_at": latest_experiment.created_at,
                    "paper_result_summary": latest_experiment.paper_result_summary,
                }
                if latest_experiment
                else None,
                "recommended_action": "run_paper_only_experiments",
                "paper_only": True,
            }
        )
    return {
        "queued": len(rows),
        "retired": sum(1 for row in rows if row["current_status"] == "retired"),
        "paused": sum(1 for row in rows if row["current_status"] == "paused"),
        "rows": rows,
    }
