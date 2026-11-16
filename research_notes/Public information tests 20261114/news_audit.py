"""Test 3a/3b: how often the app's news reader gets tone right. Frozen in PREREGISTRATION.md (same folder).

    python3 -I news_audit.py sample <work_dir>
    <repo venv> python -I news_audit.py score <work_dir> --backend <backend dir> --tag <tag> --half design|check|all

``sample`` draws 400 focused stories from the fetched archive with a fixed seed (200 from 2016-2025, 100
``fiscal.ai`` and 100 other from 2026), splits them into a design half and a check half, and writes
<work_dir>/news_audit/sample.csv **without any score**. The labels (positive, negative, neutral for the
named company) are written by hand to ``labels.csv`` before ``score`` is ever run on those rows.

``score`` runs one backend's reader (``news_discovery._article_sentiment``) on one half and compares it with
the labels. No price is read anywhere in this file.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import random
import re
import sys
from pathlib import Path

SEED = 20261114
QUOTA = {"2016_2025": 200, "2026_other": 100, "2026_fiscal": 100}
SYMBOL = re.compile(r"[A-Z]{1,5}")
MAX_SYMBOLS = 3


def read_news(work: Path):
    seen = set()
    for path in sorted((work / "news").glob("*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                item = json.loads(line)
                if item["id"] not in seen:
                    seen.add(item["id"])
                    yield item


def focused_symbols(item: dict) -> list[str]:
    """``news_discovery._article_symbols``: the named companies with a plain ticker."""
    out = []
    for value in item.get("symbols") or []:
        symbol = str(value).strip().upper()
        if SYMBOL.fullmatch(symbol) and symbol not in out:
            out.append(symbol)
    return out


def stratum(item: dict) -> str | None:
    year = item["created_at"][:4]
    if "2016" <= year <= "2025":
        return "2016_2025"
    if year == "2026":
        return "2026_fiscal" if item.get("source") == "fiscal.ai" else "2026_other"
    return None


def sample(work: Path) -> None:
    rng = random.Random(SEED)
    pools: dict[str, list] = {name: [] for name in QUOTA}
    seen = {name: 0 for name in QUOTA}
    for item in read_news(work):
        name = stratum(item)
        if name is None or not (item.get("headline") or "").strip() or not 1 <= len(focused_symbols(item)) <= MAX_SYMBOLS:
            continue
        seen[name] += 1
        pool, k = pools[name], QUOTA[name]
        if len(pool) < k:                                       # reservoir sampling: one pass, uniform
            pool.append(item)
        else:
            j = rng.randrange(seen[name])
            if j < k:
                pool[j] = item
    rows = []
    for name in QUOTA:
        pool = sorted(pools[name], key=lambda item: item["id"])
        rng.shuffle(pool)
        for position, item in enumerate(pool):
            rows.append({"id": item["id"], "stratum": name, "half": "design" if position % 2 == 0 else "check",
                         "created_at": item["created_at"], "source": item.get("source") or "",
                         "symbols": " ".join(focused_symbols(item)), "headline": item["headline"],
                         "summary": item.get("summary") or ""})
    out = work / "news_audit"
    out.mkdir(exist_ok=True)
    with (out / "sample.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"focused_stories": seen, "sampled": {name: len(pools[name]) for name in QUOTA}}))


def score(work: Path, backend: Path, tag: str, half: str) -> None:
    sys.path.insert(0, str(backend))
    from app.services import news_discovery

    out = work / "news_audit"
    with (out / "sample.csv").open() as handle:
        rows = [row for row in csv.DictReader(handle) if half == "all" or row["half"] == half]
    with (out / "labels.csv").open() as handle:
        labels = {row["id"]: row for row in csv.DictReader(handle)}
    table, missing = [], 0
    for row in rows:
        label = labels.get(row["id"])
        if label is None or label["label"] not in ("P", "N", "Z"):
            missing += 1
            continue
        value = float(news_discovery._article_sentiment({"headline": row["headline"], "summary": row["summary"]}))
        call = "P" if value > 0 else "N" if value < 0 else "Z"
        table.append({**row, "label": label["label"], "call": call, "score": round(value, 4)})
    n = len(table)
    agree = sum(r["label"] == r["call"] for r in table)
    cell = lambda label, call: sum(r["label"] == label and r["call"] == call for r in table)
    called = lambda call: sum(r["call"] == call for r in table)
    labelled = lambda label: sum(r["label"] == label for r in table)
    directional = labelled("P") + labelled("N")
    wrong_way = cell("P", "N") + cell("N", "P")
    result = {
        "tag": tag, "half": half, "stories": n, "unlabelled": missing,
        "agreement": round(agree / n, 4) if n else None,
        "labels": {k: labelled(k) for k in "PNZ"}, "calls": {k: called(k) for k in "PNZ"},
        "confusion_label_then_call": {f"{a}{b}": cell(a, b) for a in "PNZ" for b in "PNZ"},
        "positive_calls_right": round(cell("P", "P") / called("P"), 4) if called("P") else None,
        "negative_calls_right": round(cell("N", "N") / called("N"), 4) if called("N") else None,
        "directional_stories_called_neutral": round((cell("P", "Z") + cell("N", "Z")) / directional, 4) if directional else None,
        "directional_stories_called_the_wrong_way": round(wrong_way / directional, 4) if directional else None,
        "by_stratum": {},
    }
    for name in QUOTA:
        part = [r for r in table if r["stratum"] == name]
        if part:
            result["by_stratum"][name] = {"stories": len(part),
                                          "agreement": round(sum(r["label"] == r["call"] for r in part) / len(part), 4)}
    (out / f"score_{tag}_{half}.json").write_text(json.dumps(result, indent=1))
    with (out / f"scored_{tag}_{half}.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "stratum", "half", "source", "symbols", "label", "call", "score",
                                                    "headline"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(table)
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["sample", "score"])
    parser.add_argument("work", type=Path)
    parser.add_argument("--backend", type=Path)
    parser.add_argument("--tag")
    parser.add_argument("--half", choices=["design", "check", "all"], default="all")
    args = parser.parse_args()
    if args.stage == "sample":
        sample(args.work)
    else:
        score(args.work, args.backend.resolve(), args.tag, args.half)
