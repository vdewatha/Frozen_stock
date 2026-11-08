"""Step 3's pre-registered test: settle the defensive sleeve (``reports/defensive-sleeve-20261107.md``).

Runs ``app.services.defensive_sleeve_rule`` on a cached price frame (the
``px.csv`` that ``research_notes/Retail bot edge feasibility/etf_rotation_backtest.py``
writes: yfinance adjusted closes, $0 data) and prints:

1. the decision table: Designs A and B, tranched, at 11 and 25 bps a side, over
   2008-07 to latest and 2015 to latest, as 10/20/30% slices from the T-bill leg
   of the live 20/80 arm (primary) and of the report's 50/50 arm (beside it);
2. a reproduction of the report's blend check (50/50, data to 2026-10-31);
3. volatility-matched static SPY/T-bill arms: matched to each design alone and
   to each 20% slice arm;
4. the ETF note's untested criterion 3 (reported, not decisive): a 50/50 blend
   of each design with the replayed book's daily P&L (today's 45 names, live
   slots at 5% of equity, the published cost model; ``--book-data-dir`` is the
   yardstick's yfinance cache) against the book alone.

    PYTHONPATH=. python scripts/run_defensive_sleeve_check.py --px <px.csv> \\
        [--book-data-dir <dir>] --output <json>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import defensive_sleeve_rule as R  # noqa: E402


def _pct(x, d=1):
    return f"{x * 100:+.{d}f}%" if x < 0 else f"{x * 100:.{d}f}%"


def decision_table(result: dict, label: str) -> list[str]:
    lines = [f"\n=== {label}: arm {result['equity_weight']:.0%} SPY / rest BIL; slices from the T-bill leg "
             f"(data to {result['data_end']}) ==="]
    for design, body in result["designs"].items():
        lines.append(f"{R.DESIGN_NAMES[design]}: rule {'PASSES' if body['passes'] else 'FAILS'}")
        for key, cell in body["cells"].items():
            b = cell["base"]
            parts = [f"base Sh {b['sharpe']:.3f} DD {_pct(b['max_drawdown'])} CAGR {_pct(b['cagr'], 2)}"]
            for x, s in cell["slices"].items():
                parts.append(f"{float(x):.0%}: Sh {s['sharpe']:.3f} ({s['sharpe_change']:+.3f}) DD {_pct(s['max_drawdown'])} "
                             f"({s['extra_drawdown_points']:+.1f} pts) CAGR {_pct(s['cagr'], 2)}"
                             f"{' pass' if s['passes'] else ' fail'}")
            sa = cell["standalone"]
            lines.append(f"  [{key}] " + " | ".join(parts)
                         + f" || alone: CAGR {_pct(sa['cagr'], 2)} vol {_pct(sa['vol'])} Sh {sa['sharpe']:.2f} DD {_pct(sa['max_drawdown'])}")
    return lines


def static_matched(px: pd.DataFrame, end=None) -> tuple[dict, list[str]]:
    """Static SPY/BIL arms (monthly, 11 bps) at the volatility of each design alone and of each 20% slice arm."""
    px = px[list(R.SYMBOLS)].dropna(how="all").ffill().dropna()
    rets = px.pct_change().fillna(0.0)
    rf = rets[R.CASH]
    days = [d for o in R.TRANCHE_OFFSETS for d in R.month_end_days(px.index, o)]
    out, lines = {}, ["\n=== Volatility-matched static SPY/T-bill arms (monthly rebalanced, 11 bps a side) ==="]
    for design in (R.DESIGN_A, R.DESIGN_B):
        sig = R.signal_table(px, design, days)
        stream = R.tranched(px, design, 11.0, signals=sig)
        for label, start in R.WINDOW_STARTS.items():
            spy = R.stats(rets[R.EQUITY], rf, start, end)
            alone = R.stats(R.tranched(px, design, 11.0, start=start, signals=sig), rf, start, end)
            for target_name, target in (("design alone", alone),
                                        ("20/80 arm + 20% slice", R.stats(R.arm(px, stream, equity_weight=0.2, slice_=0.2), rf, start, end)),
                                        ("50/50 arm + 20% slice", R.stats(R.arm(px, stream, equity_weight=0.5, slice_=0.2), rf, start, end))):
                w = min(1.0, target["vol"] / spy["vol"])
                static = R.stats(R.arm(px, None, equity_weight=w, slice_=0), rf, start, end)
                key = f"{design}|{label}|{target_name}"
                out[key] = {"target": target, "spy_weight": w, "static": static}
                lines.append(f"  {R.DESIGN_NAMES[design]} {label} matched to {target_name}: target CAGR {_pct(target['cagr'], 2)} "
                             f"vol {_pct(target['vol'])} Sh {target['sharpe']:.3f} DD {_pct(target['max_drawdown'])} || static "
                             f"{w:.0%} SPY: CAGR {_pct(static['cagr'], 2)} Sh {static['sharpe']:.3f} DD {_pct(static['max_drawdown'])}")
    return out, lines


def book_series(data_dir: Path, start: str) -> tuple[pd.Series, pd.Series]:
    """The replayed live book's daily return (model cost, idle cash at 0) and its gross exposure, trades decided from ``start``."""
    import run_regime_allocation_backtest as regime
    import run_position_sizing_backtest as sizing
    from run_slot_allocation_backtest import allocate
    from run_yardstick_check import book_daily
    from app.core.config import settings
    from app.services import gate_replay
    from app.services import meta_label_primary as primary

    settings.meta_label_history_dir = str(data_dir)
    gate_replay._calibration_cache.update(at=time.monotonic() + 1e9, value=None)  # published model, no database
    frames, earnings = regime.load_frames(data_dir, download=False, start="2000-01-01", end=None)
    vix_path = data_dir / "VIX_close.csv"
    vix = pd.read_csv(vix_path) if vix_path.exists() else None
    spy = frames["SPY"]
    replays = {s: primary.replay_signals(f) for s, f in frames.items()}
    rows = regime.candidates(frames, spy, vix, replays, earnings=earnings)
    accepted = allocate(rows, frames, regime.BOOK_POLICY).reset_index(drop=True)
    book = sizing.Book(accepted, frames)
    weights = np.full(len(accepted), sizing.SLOT)
    daily = book_daily(book, accepted, weights, start)
    w = weights.copy()
    w[book.decision < np.datetime64(pd.Timestamp(start))] = 0.0
    exposure = pd.Series(np.bincount(book.day_ids, weights=w[book.trade_ids], minlength=len(book.dates)),
                         index=pd.DatetimeIndex(book.dates)).reindex(daily.index).fillna(0.0)
    return daily, exposure


def criterion_3(px: pd.DataFrame, data_dir: Path, end=None) -> tuple[dict, list[str]]:
    px = px[list(R.SYMBOLS)].dropna(how="all").ffill().dropna()
    rets = px.pct_change().fillna(0.0)
    days = [d for o in R.TRANCHE_OFFSETS for d in R.month_end_days(px.index, o)]
    sigs = {d: R.signal_table(px, d, days) for d in (R.DESIGN_A, R.DESIGN_B)}
    out, lines = {}, ["\n=== Criterion 3 (reported, not decisive): 50/50 blend with the replayed book's daily P&L "
                      "(45 names, 5% slots, published cost model) ==="]
    for label, start in R.WINDOW_STARTS.items():
        daily, exposure = book_series(data_dir, start)
        idx = daily.index.intersection(px.index)
        daily, exposure = daily.reindex(idx), exposure.reindex(idx)
        bil = rets[R.CASH].reindex(idx).fillna(0.0)
        variants = {"idle cash at 0 (as replayed)": daily,
                    "idle cash at T-bills": daily + (1 - exposure.clip(upper=1.0)) * bil}
        for vname, book in variants.items():
            b = R.stats(book, bil, start, end)
            lines.append(f"  [{label}] book, {vname}: CAGR {_pct(b['cagr'], 2)} Sh {b['sharpe']:.3f} DD {_pct(b['max_drawdown'])} "
                         f"(avg exposure {exposure.loc[start:].mean():.0%})")
            for design in (R.DESIGN_A, R.DESIGN_B):
                for cost in R.COSTS_BPS:
                    stream = R.tranched(px, design, cost, signals=sigs[design]).reindex(idx).fillna(0.0)
                    blend = 0.5 * book + 0.5 * stream  # daily-rebalanced, the note's criterion
                    s = R.stats(blend, bil, start, end)
                    passes = s["max_drawdown"] > b["max_drawdown"] and s["sharpe"] >= b["sharpe"]
                    out[f"{label}|{vname}|{design}|{cost:g}"] = {"book": b, "blend": s, "passes": passes}
                    lines.append(f"      + 50% {R.DESIGN_NAMES[design]} @{cost:g}bp: CAGR {_pct(s['cagr'], 2)} Sh {s['sharpe']:.3f} "
                                 f"DD {_pct(s['max_drawdown'])} -> {'meets' if passes else 'misses'} criterion 3")
    return out, lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--px", type=Path, required=True)
    parser.add_argument("--book-data-dir", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    warnings.filterwarnings("ignore")
    started = time.time()
    px = pd.read_csv(args.px, index_col=0, parse_dates=True)
    result, lines = {"rule": R.RULE}, [R.RULE]
    for weight, label in ((0.20, "PRIMARY, live 20/80 arm"), (0.50, "BESIDE IT, the report's 50/50 arm")):
        result[f"arm_{weight:.2f}"] = r = R.evaluate(px, equity_weight=weight)
        lines += decision_table(r, label)
    repro = R.evaluate(px, end="2026-10-31", equity_weight=0.5, designs=[R.DESIGN_A])
    repro["data_end"] = "2026-10-31 (window end; prices to 2026-11-06)"
    result["reproduction_50_50_to_2026_10_31"] = repro
    lines += decision_table(repro, "REPRODUCTION of blend_check.py (50/50, Design A)")
    result["static_matched"], extra = static_matched(px)
    lines += extra
    if args.book_data_dir is not None:
        result["criterion_3"], extra = criterion_3(px, args.book_data_dir)
        lines += extra
    result["elapsed_seconds"] = round(time.time() - started)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=1, default=str) + "\n")
    args.output.with_suffix(".txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print("wrote", args.output, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
