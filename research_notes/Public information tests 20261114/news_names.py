"""Is a story about the company its ticker names?  Text and issuer names only; no price is read.

    python3 -I news_names.py names <work_dir> <price cache dir> <SEC submissions.zip>
    <repo venv> python -I news_names.py check <work_dir> [--tag fixed] [--year 2026]

``names`` writes <work_dir>/news_rules/issuer_names.csv: the SEC name and former names of every issuer with a
series in the price cache.  ``check`` takes every (story, stock) pair the tone stage mapped in one year and asks
whether the story's headline or summary mentions the issuer (a distinctive word of its current or a former
name), or the ticker written the way US news writes it ("(NASDAQ: ABCD)", "NYSE:ABCD", "$ABCD").  It reports
the share of pairs with neither, by source: a story about a brand or a subsidiary fails this test too, so the
Benzinga rate is the false-alarm floor and only the gap between sources means anything.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import random
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

SEED = 20261114
GENERIC = {"the", "inc", "incorporated", "corp", "corporation", "co", "company", "companies", "ltd", "limited", "plc",
           "holdings", "holding", "group", "sa", "nv", "ag", "se", "lp", "llc", "trust", "fund", "partners", "class",
           "common", "stock", "american", "america", "first", "national", "united", "general", "international",
           "global", "new", "north", "south", "east", "west", "western", "eastern", "northern", "southern", "bank",
           "bancorp", "bancshares", "financial", "capital", "energy", "resources", "technologies", "technology",
           "systems", "industries", "pharmaceuticals", "therapeutics", "biosciences", "sciences", "medical", "health",
           "healthcare", "and", "of", "de", "us", "usa", "one", "home", "life", "real", "estate", "properties",
           "property", "realty", "insurance", "services", "solutions", "enterprises", "investment", "investments",
           "acquisition", "communications", "entertainment", "brands", "foods", "air", "airlines", "oil", "gas",
           "gold", "mining", "minerals", "water", "power", "electric", "motor", "motors", "digital", "data", "media",
           "royal", "great", "big", "blue", "green", "red", "black", "white", "star", "sun", "city", "state"}
WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    return WORD.findall(text.lower().replace("&#39;", "'").replace("&amp;", "&").replace("'s", ""))


def keys_of(name: str) -> list[tuple[str, ...]]:
    """What must appear in the text: the first distinctive word, or the first two words when it has none."""
    tokens = words(name)
    distinctive = [t for t in tokens if t not in GENERIC and len(t) >= 3]
    keys = []
    if distinctive:
        keys.append((distinctive[0],))
    if len(tokens) >= 2:
        keys.append(tuple(tokens[:2]))
    return keys


def stage_names(work: Path, cache: Path, archive: Path) -> None:
    ciks = set()
    with gzip.open(cache / "identity.csv.gz", "rt") as handle:
        for row in csv.DictReader(handle):
            if row["kept"] == "True":
                ciks.add(row["cik"])
    out = work / "news_rules"
    out.mkdir(exist_ok=True)
    found = 0
    with zipfile.ZipFile(archive) as bundle, (out / "issuer_names.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["cik", "name", "former_names"])
        members = set(bundle.namelist())
        for cik in sorted(ciks):
            member = f"CIK{cik}.json"
            if member not in members:
                continue
            data = json.loads(bundle.read(member))
            former = [str(item.get("name") or "") for item in data.get("formerNames") or []]
            writer.writerow([cik, data.get("name") or "", "|".join(name for name in former if name)])
            found += 1
    print(json.dumps({"issuers": len(ciks), "named": found}))


def stage_check(work: Path, tag: str, year: str) -> None:
    import pandas as pd

    out = work / "news_rules"
    names = {}
    with (out / "issuer_names.csv").open() as handle:
        for row in csv.DictReader(handle):
            keys = []
            for name in [row["name"], *[n for n in row["former_names"].split("|") if n]]:
                keys.extend(keys_of(name))
            names[row["cik"]] = (row["name"], keys)
    stories = pd.read_pickle(out / f"stories_{tag}.pkl.gz")
    stories = stories[stories["session"].str[:4] == year]
    by_id = defaultdict(list)
    for series, story in zip(stories["series"], stories["id"]):
        by_id[int(story)].append(series)
    counts: dict = defaultdict(Counter)
    misses = defaultdict(list)
    for path in sorted((work / "news").glob(f"{year}-*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                item = json.loads(line)
                mapped = by_id.get(int(item["id"]))
                if not mapped:
                    continue
                raw = f"{item.get('headline') or ''} {item.get('summary') or ''}"
                tokens = words(raw)
                have = set(tokens)
                pairs = set(zip(tokens, tokens[1:]))
                source = item.get("source") or "benzinga"
                for series in mapped:
                    cik, ticker = series.split(":", 1)
                    issuer, keys = names.get(cik, ("", []))
                    by_name = any((key[0] in have) if len(key) == 1 else (key in pairs) for key in keys)
                    by_ticker = bool(re.search(rf"(?:NYSE|NASDAQ|AMEX|ARCA|BATS|OTC\w*)\s*:\s*{re.escape(ticker)}\b|\${re.escape(ticker)}\b|\({re.escape(ticker)}\)",
                                               raw, re.IGNORECASE))
                    kind = "name" if by_name else "ticker_only" if by_ticker else "neither"
                    counts[source][kind] += 1
                    if kind == "neither":
                        misses[source].append((ticker, issuer, item["headline"]))
    rng = random.Random(SEED)
    summary = {"year": year, "tag": tag, "by_source": {}}
    for source, c in counts.items():
        total = sum(c.values())
        summary["by_source"][source] = {"pairs": total, **{k: c[k] for k in ("name", "ticker_only", "neither")},
                                        "share_neither": round(c["neither"] / total, 4)}
        sample = rng.sample(misses[source], min(60, len(misses[source])))
        with (out / f"name_misses_{source.replace('.', '_')}_{year}.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ticker", "issuer_in_price_cache", "headline"])
            writer.writerows(sample)
    (out / f"name_check_{year}.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["names", "check"])
    parser.add_argument("work", type=Path)
    parser.add_argument("rest", nargs="*")
    parser.add_argument("--tag", default="fixed")
    parser.add_argument("--year", default="2026")
    args = parser.parse_args()
    if args.stage == "names":
        stage_names(args.work, Path(args.rest[0]), Path(args.rest[1]))
    else:
        stage_check(args.work, args.tag, args.year)
