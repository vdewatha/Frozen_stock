"""Unfaked end-to-end run of the experiment engine on real history (local only; never the deployed app)."""
import json, logging, resource, sys, tempfile, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.db.base import Base
from app.models import Strategy
from app.services import experiment_engine as engine
from tests.test_experiment_engine import _attribution

NOW = datetime(2026, 11, 11, 21, 30, tzinfo=timezone.utc)
tmp = tempfile.TemporaryDirectory()
db = Session(create_engine("sqlite:///" + str(Path(tmp.name) / "e2e.sqlite")))
Base.metadata.create_all(db.get_bind())
for slug in ("channel_breakout", "bollinger_mean_reversion", "rsi_mean_reversion", "ensemble", "trend_pullback",
             "rsi2_pullback"):
    db.add(Strategy(name=slug, strategy_type=slug, parameters={}, current_status="shadow", is_active=True))
db.add(Strategy(name="frozen", strategy_type="rule_abc123", parameters={"forward_experiment": {"candidate": 1}},
                current_status="paper_trading_candidate", is_active=True))
db.flush()
for i in range(14):   # the 11-05 picture: the whole market fell and took a cluster of positions with it
    _attribution(db, strategy_type="channel_breakout" if i % 2 else "bollinger_mean_reversion",
                 cause="missed_market_move", net_return=-0.025, closed_at=NOW - timedelta(days=6, minutes=i),
                 contributing=["market_down", "clustered_stops"])
db.commit()
started = time.time()
result = engine.run_experiment_engine(db, now=NOW)
db.commit()
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 ** 2 if sys.platform == "darwin" else 1024)
print("took", round(time.time() - started), "s; peak RSS", round(peak), "MB")
print("learning_set", result["learning_set"])
print("history", json.dumps(result["history"])[:600])
print("planned", result["planned"], "deferred", len(result["deferred"]), "skipped", len(result["skipped"]))
print("errors", result["errors"])
for e in result["experiments"]:
    print("experiment", {k: e.get(k) for k in ("strategy_type", "template", "trigger", "adopted", "adopted_description", "applied")})
ledger = engine.experiment_ledger(db)
for e in ledger["experiments"]:
    rows = e.get("rows") or {}
    print(e["strategy_type"], e["template"], "universe", (e.get("universe") or {}).get("mode"), "symbols", e.get("symbols"))
    for label, windows in rows.items():
        w = windows.get("all") or {}
        print("   ", label, "trades", w.get("trades"), "exp_bps", w.get("expectancy_bps"), "dd", w.get("portfolio_max_drawdown"),
              "| 2015+", (windows.get("2015+") or {}).get("expectancy_bps"), "| 2020+", (windows.get("2020+") or {}).get("expectancy_bps"))
    d = e.get("decision") or {}
    print("    adopted:", d.get("adopted"), "| failed:", json.dumps(d.get("failed"))[:400], "| hurdle:", d.get("blocked_by_hurdle"))
    if e.get("book"):
        print("    book strategies:", e["book"].get("strategies"), "candidate trades", e["book"].get("candidate_trades"))
print("changes", [(c["strategy_type"], c["description"], c["state"]) for c in ledger["changes"]])
