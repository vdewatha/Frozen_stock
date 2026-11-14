"""Test 3c: the app's twelve news rules on the whole market. Frozen in PREREGISTRATION.md (same folder).

    <repo venv> python -I news_rules.py tone     <work_dir> <price cache dir> --backend <backend dir> --tag <tag>
    <repo venv> python -I news_rules.py evaluate <work_dir> <price cache dir> --backend <backend dir> --tag <tag> [--workers 8]

``--backend`` is the app code whose reader scores the stories (``news_discovery._article_sentiment``) and
whose rule engine and labeler replay the rules; ``--tag`` names the output (``fixed`` for the reader frozen
in 3b, ``live`` for the reader of commit a530b16).

* ``tone`` reads every fetched story (``news_fetch.py``), scores it, maps each named company to its series in
  PR #95's price cache by ticker on the story's session, and writes the per-series daily tone
  (<work_dir>/news_rules/tone_<tag>.pkl.gz) and the per-story scores (stories_<tag>.pkl.gz). No return is read.
* ``evaluate`` attaches the tone to each series, replays the twelve rules with the app's rule engine and
  labeler (PR #95's method) at 25 and 12.5 bps a side, and writes trades_<tag>.csv.gz and results_<tag>.json.
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pubinfo_lib as L  # noqa: E402

DIP = HERE.parent / "Dip rules full market 20261112" / "dip_rules_check.py"
T_REQUIRED = 2.87                       # two-sided 5% over twelve rules
COSTS_BPS = (25.0, 12.5)                # a side; the first is decisive
OPEN_EXITS = {"stop_loss_gap", "vertical", "indicator", "take_profit_gap"}
MAX_SYMBOLS_PER_ARTICLE = 3
FISCAL_SOURCE = "fiscal.ai"


def _dip():
    spec = importlib.util.spec_from_file_location("dip_rules_check", DIP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_news(work: Path):
    """Every fetched story once (by id), oldest first."""
    seen = set()
    for path in sorted((work / "news").glob("*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                item = json.loads(line)
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                yield item


def stage_tone(work: Path, cache: Path, backend: Path, tag: str) -> None:
    sys.path.insert(0, str(backend))
    from app.services import news_discovery, news_history

    market = L.load_market(cache)
    calendar = market.calendar
    out = work / "news_rules"
    out.mkdir(exist_ok=True)
    rows, total, focused, unmapped = [], 0, 0, 0
    started = time.time()
    for item in read_news(work):
        total += 1
        symbols = news_discovery._article_symbols(item)
        headline = item.get("headline") or ""
        if not headline or not 1 <= len(symbols) <= MAX_SYMBOLS_PER_ARTICLE:
            continue
        focused += 1
        published = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
        session = news_history.session_day(published).isoformat()
        at = int(np.searchsorted(calendar, session))            # the first bar on or after that session
        if at >= len(calendar):
            continue
        score = float(news_discovery._article_sentiment(item))
        for symbol in symbols:
            s = L.series_by_ticker(market, symbol, at)
            if s is None:
                unmapped += 1
                continue
            rows.append((s, session, at, score, item["id"], item.get("source") or "", len(symbols)))
        if total % 500000 == 0:
            print(f"{total} stories read ({time.time() - started:.0f} s)", flush=True)
    stories = pd.DataFrame(rows, columns=["series", "session", "bar", "score", "id", "source", "named"])
    stories.to_pickle(out / f"stories_{tag}.pkl.gz")
    # news_history.aggregate_daily, for every series at once: mean over the directional stories, 0 without one.
    stories["directional"] = (stories["score"] != 0).astype(int)
    daily = stories.groupby(["series", "session"]).agg(articles=("score", "size"), directional=("directional", "sum"),
                                                        weighted=("score", "sum")).reset_index()
    daily["tone"] = np.where(daily["directional"] > 0, daily["weighted"] / daily["directional"].clip(lower=1), 0.0)
    daily = daily.rename(columns={"session": "date"})[["series", "date", "tone", "articles", "directional"]]
    daily.to_pickle(out / f"tone_{tag}.pkl.gz")
    # The app's own function on a sample of series must give the same rows.
    check = stories[stories["series"].isin(stories["series"].drop_duplicates().head(50))]
    for s, part in check.groupby("series"):
        ours = daily[daily["series"] == s].reset_index(drop=True)
        pairs = [(datetime.fromisoformat(d + "T12:00:00+00:00"), sc) for d, sc in zip(part["session"], part["score"])]
        theirs = news_history.aggregate_daily(pairs)
        assert len(ours) == len(theirs) and np.allclose(ours["tone"], theirs["tone"]) \
            and (ours["articles"].to_numpy() == theirs["articles"].to_numpy()).all(), f"tone mismatch for series {s}"
    summary = {"stories": total, "focused": focused, "story_stock_pairs": int(len(stories)), "unmapped_pairs": unmapped,
               "series_with_news": int(stories["series"].nunique()), "series_sessions": int(len(daily)),
               "share_positive": float((stories["score"] > 0).mean()), "share_negative": float((stories["score"] < 0).mean()),
               "share_neutral": float((stories["score"] == 0).mean()),
               "by_year": {y: int(n) for y, n in stories["session"].str[:4].value_counts().sort_index().items()}}
    (out / f"tone_{tag}.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


# ------------------------------------------------------------------ replay (PR #95's worker, with news columns)

STATE: dict = {}


def _init(state: dict, backend: str) -> None:
    sys.path.insert(0, backend)
    STATE.update(state)


def replay(item) -> dict:
    from app.services import news_history
    from app.services.strategies import rules
    from app.services.strategy_discovery import _executable
    from app.services.triple_barrier import BarrierConfig, label_events

    name, frame, blocked, tone = item
    frame = news_history.attach_tone(frame.reset_index(drop=True), tone)
    n = len(frame)
    out = {"series": name, "trades": [], "news_bars": int((frame["news_articles"] > 0).sum())}
    dates = frame["date"].to_numpy(str)
    close = frame["close"].to_numpy(float)
    raw_close = frame["raw_close"].to_numpy(float)
    dollar = pd.Series(raw_close * frame["raw_volume"].to_numpy(float))
    median_dv = dollar.rolling(L.DV_WINDOW, min_periods=L.DV_WINDOW).median().to_numpy()
    eligible = np.isfinite(raw_close) & (raw_close >= L.MIN_PRICE) & np.isfinite(median_dv) \
        & (median_dv >= L.MIN_DOLLAR_VOLUME)
    if blocked:
        eligible &= ~np.isin(np.array([d[:4] for d in dates]), list(blocked))
    if n < 25 or not eligible.any() or not out["news_bars"]:
        return out
    delisted = dates[-1] < STATE["alive_from"]
    pct = np.diff(close) / close[:-1]
    drift = np.concatenate([[np.nan], np.cumsum(pct) / np.arange(1, n)])
    spy_open, spy_close = STATE["spy_open"], STATE["spy_close"]
    cache: dict = {}
    for key, rule, plan in STATE["rules"]:
        entry = np.asarray(rules.entry_signals(frame, rule, cache), dtype=bool)
        exits = np.asarray(rules.exit_signals(frame, rule, cache), dtype=bool)
        events = np.flatnonzero(entry & eligible)
        if not len(events):
            continue
        for cost in COSTS_BPS:
            config = BarrierConfig(take_profit_pct=plan.get("take_profit_pct", 0.0), stop_loss_pct=plan["stop_loss_pct"],
                                   max_holding_days=plan["max_holding_sessions"], cost_model="flat",
                                   slippage_bps=cost, fees_bps=0.0)
            labels = label_events(frame, events, config, exit_signals=exits)
            labels = labels.assign(barrier=labels["barrier"].astype(object)) if len(labels) else labels
            extra = []
            if delisted:                                        # an unresolved trade in a stock that stopped trading
                resolved = set(labels["event_idx"].tolist()) if len(labels) else set()
                for t in events:
                    t = int(t)
                    entry_idx = t + 1
                    if t in resolved or entry_idx >= n:
                        continue
                    open_entry = float(frame.at[entry_idx, "open"])
                    if not (math.isfinite(open_entry) and open_entry > 0):
                        continue
                    entry_price = open_entry * (1 + cost / 1e4)
                    exit_price = close[n - 1] * (1 - cost / 1e4)
                    extra.append({"event_idx": t, "entry_idx": entry_idx, "exit_idx": n - 1, "decision_date": dates[t],
                                  "entry_date": dates[entry_idx], "exit_date": dates[n - 1], "entry_price": entry_price,
                                  "exit_price": exit_price, "barrier": "delisted_last_close",
                                  "holding_days": n - 1 - entry_idx, "gross_return": close[n - 1] / open_entry - 1,
                                  "net_return": exit_price / entry_price - 1})
            if extra:
                labels = pd.concat([labels, pd.DataFrame(extra)], ignore_index=True) if len(labels) else pd.DataFrame(extra)
            if not len(labels):
                continue
            labels = _executable(labels.sort_values("entry_idx").reset_index(drop=True))
            for row in labels.itertuples(index=False):
                spy_in = spy_open.get(row.entry_date)
                spy_out = (spy_open if row.barrier in OPEN_EXITS else spy_close).get(row.exit_date)
                spy_ret = spy_out / spy_in - 1 if spy_in and spy_out else np.nan
                hold = max(int(row.holding_days), 1)
                d = drift[int(row.event_idx)]
                out["trades"].append({
                    "rule": key, "cost_bps": cost, "series": name, "decision_date": row.decision_date,
                    "entry_date": row.entry_date, "exit_date": row.exit_date, "barrier": row.barrier,
                    "holding_days": int(row.holding_days), "gross": float(row.gross_return),
                    "net": float(row.net_return), "spy": spy_ret,
                    "excess": float(row.net_return) - spy_ret if np.isfinite(spy_ret) else np.nan,
                    "excess_drift": float(row.net_return) - hold * (0.0 if not np.isfinite(d) else float(d)),
                    "delisted_since": bool(delisted)})
    return out


def news_rules(backend: Path) -> list[tuple[str, dict, dict]]:
    sys.path.insert(0, str(backend))
    from app.services import strategy_discovery as sd

    counts: dict = defaultdict(int)
    out = []
    for family, _hypothesis, rule, plan in sd._families():
        if family not in sd.NEWS_FAMILIES:
            continue
        counts[family] += 1
        out.append((f"{family}#{counts[family]}", rule, plan))
    return out


def describe_rule(rule: dict, plan: dict) -> str:
    text = lambda conditions: " and ".join(f"{c['f']} {c['op']} {c['v']:g}" for c in conditions) or "none"
    return f"buy: {text(rule['entry'])}; sell: {text(rule.get('exit', []))} or {plan['max_holding_sessions']} sessions; " \
           f"stop {plan['stop_loss_pct']:.0%}"


def stage_evaluate(work: Path, cache: Path, backend: Path, tag: str, workers: int) -> None:
    from multiprocessing import Pool

    started = time.time()
    dip = _dip()
    out = work / "news_rules"
    rules_list = news_rules(backend)
    assert len(rules_list) == 12, f"expected the app's twelve news rules, found {len(rules_list)}"
    from app.services.trading_universe import DEFAULT_PAPER_EXECUTION_SYMBOLS, ETF_SYMBOLS

    own = {symbol for symbol in DEFAULT_PAPER_EXECUTION_SYMBOLS if symbol not in ETF_SYMBOLS}
    spy = pd.read_pickle(cache / "spy.pkl.gz")
    spy = spy[spy["date"] <= L.LAST_SESSION].reset_index(drop=True)
    calendar = spy["date"].tolist()
    state = {"spy_open": dict(zip(spy["date"], spy["open"].astype(float))),
             "spy_close": dict(zip(spy["date"], spy["close"].astype(float))),
             "alive_from": calendar[-L.ALIVE_SESSIONS], "rules": rules_list}
    identity = pd.read_csv(cache / "identity.csv.gz", dtype=str, keep_default_na=False)
    kept = identity[identity["kept"] == "True"]
    names = list(kept["series"])                               # the row order of pubinfo_lib.load_market
    blocked = {name: {y for y in years.split(",") if y} for name, years in zip(kept["series"], kept["blocked_years"])}
    bars, _series = dip.load_series(cache)
    tone = pd.read_pickle(out / f"tone_{tag}.pkl.gz")
    tone_of = {names[int(s)]: part[["date", "tone", "articles", "directional"]].reset_index(drop=True)
               for s, part in tone.groupby("series")}
    items = [(name, bars[name], blocked[name], tone_of[name]) for name in names if name in bars and name in tone_of]
    print(f"{len(names)} series, {len(items)} with news ({time.time() - started:.0f} s)", flush=True)
    results = []
    with Pool(workers, initializer=_init, initargs=(state, str(backend))) as pool:
        for i, result in enumerate(pool.imap_unordered(replay, items, chunksize=8)):
            results.append(result)
            if (i + 1) % 1000 == 0:
                print(f"{i + 1}/{len(items)} series replayed ({time.time() - started:.0f} s)", flush=True)
    trades = pd.DataFrame([t for r in results for t in r["trades"]])
    trades["ticker"] = trades["series"].str.split(":").str[1]
    trades["month"], trades["year"] = trades["decision_date"].str[:7], trades["decision_date"].str[:4]
    trades = trades.dropna(subset=["excess"]).sort_values(["rule", "cost_bps", "decision_date", "series"]).reset_index(drop=True)
    trades.to_csv(out / f"trades_{tag}.csv.gz", index=False)

    first_fiscal = None
    stories = pd.read_pickle(out / f"stories_{tag}.pkl.gz")
    fiscal = stories[stories["source"] == FISCAL_SOURCE]
    if len(fiscal):
        first_fiscal = str(fiscal["session"].min())
    summary = {"tag": tag, "data_end": calendar[-1], "t_required": T_REQUIRED, "series_with_news": len(items),
               "first_fiscal_session": first_fiscal, "rules": {}}
    passed = []
    for key, rule, plan in rules_list:
        part = trades[(trades["rule"] == key) & (trades["cost_bps"] == COSTS_BPS[0])]
        stats = L.summarize(part, T_REQUIRED)
        entry = {"rule": describe_rule(rule, plan), "25bps": stats}
        if stats.get("trades"):
            drift = part["excess_drift"].to_numpy(float)
            stats["excess_drift_bps"] = float(drift.mean() * 1e4)
            stats["excess_drift_t"] = L.clustered_t(drift, part["month"].to_numpy())
            stats["mean_hold"] = float(part["holding_days"].mean())
            stats["share_delisted_since"] = float(part["delisted_since"].mean())
            stats["passes_with_drift"] = bool(stats["passes"] and stats["excess_drift_bps"] > 0)
            if stats["passes_with_drift"]:
                passed.append(key)
            lenient = L.summarize(trades[(trades["rule"] == key) & (trades["cost_bps"] == COSTS_BPS[1])], T_REQUIRED)
            entry["12.5bps"] = {k: lenient.get(k) for k in ("trades", "net_bps", "net_t", "excess_bps", "excess_t", "passes")}
            inside = L.summarize(part[part["ticker"].isin(own)], T_REQUIRED)
            entry["bot_29_stocks"] = {k: inside.get(k) for k in ("trades", "net_bps", "net_t", "excess_bps", "excess_t")}
            if first_fiscal:
                for label, sub in (("before_fiscal", part[part["decision_date"] < first_fiscal]),
                                   ("since_fiscal", part[part["decision_date"] >= first_fiscal])):
                    s = L.summarize(sub, T_REQUIRED)
                    entry[label] = {k: s.get(k) for k in ("trades", "net_bps", "net_t", "excess_bps", "excess_t")}
        summary["rules"][key] = entry
    summary["passed"] = passed
    summary["verdict"] = "forward paper test" if passed else "closed"
    (out / f"results_{tag}.json").write_text(json.dumps(summary, indent=1, default=str))
    for key, entry in summary["rules"].items():
        s = entry["25bps"]
        print(f"{key:24s} trades {s.get('trades', 0):7d} net {L.fmt(s.get('net_bps'))} (t {L.fmt(s.get('net_t'), 2)}) "
              f"vs SPY {L.fmt(s.get('excess_bps'))} (t {L.fmt(s.get('excess_t'), 2)}) "
              f"vs drift {L.fmt(s.get('excess_drift_bps'))} pass {s.get('passes_with_drift', False)}", flush=True)
    print(json.dumps({"passed": passed, "verdict": summary["verdict"]}), f"({time.time() - started:.0f} s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["tone", "evaluate"])
    parser.add_argument("work", type=Path)
    parser.add_argument("cache", type=Path)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if args.stage == "tone":
        stage_tone(args.work, args.cache, args.backend.resolve(), args.tag)
    else:
        stage_evaluate(args.work, args.cache, args.backend.resolve(), args.tag, args.workers)
