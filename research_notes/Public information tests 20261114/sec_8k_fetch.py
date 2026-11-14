"""Test 1 inputs: every 8-K of the issuers in the ticker map, with its items and its acceptance time.

    python3 -I sec_8k_fetch.py filings <work_dir> <submissions.zip> <ticker_map.csv.gz>
    SEC_USER_AGENT="<who you are> <contact email>" python3 -I sec_8k_fetch.py acceptance <work_dir> <earlier acceptance.csv> <earlier acceptance.done>

``filings`` reads the SEC's bulk submissions file already on disk (no network) and writes
<work_dir>/sec/filings_8k.csv: cik, accession, filed, items (Item 9.01 kept; the evaluation ignores it).

``acceptance`` is the only code of this test that talks to sec.gov. The bulk file's acceptance times mix
three clocks (PR #92), so the time of each filing comes from the live submissions API
(https://data.sec.gov/submissions/CIK##########.json and its older-filings files), which serves UTC. Times
PR #92 already fetched for 6,996 issuers are copied; the other issuers are fetched here, about five requests
a second, under the SEC's limit of ten. The SEC refuses scripts that do not declare a contact, so the
User-Agent comes from the environment and is not stored here. No price is read.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REQUEST_INTERVAL = 0.2
WORKERS = 4
FIRST_FILED = "2015-12-25"

_gate = threading.Lock()
_next_request = [0.0]


def filings(work: Path, bulk: Path, ticker_map: Path) -> None:
    with gzip.open(ticker_map, "rt") as handle:
        issuers = {row["cik"] for row in csv.DictReader(handle)}
    archive = zipfile.ZipFile(bulk)
    (work / "sec").mkdir(parents=True, exist_ok=True)
    rows = set()
    for name in archive.namelist():
        cik = name[3:13]
        if not name.startswith("CIK") or cik not in issuers:
            continue
        with archive.open(name) as handle:
            body = json.load(handle)
        block = body["filings"]["recent"] if "filings" in body else body
        for form, items, accession, filed in zip(block["form"], block["items"], block["accessionNumber"],
                                                 block["filingDate"]):
            if form == "8-K" and filed >= FIRST_FILED:
                listed = sorted({item.strip() for item in (items or "").split(",") if item.strip()})
                rows.add((cik, accession, filed, " ".join(listed)))
    with (work / "sec" / "filings_8k.csv").open("w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["cik", "accession", "filed", "items"])
        writer.writerows(sorted(rows))
    print(f"{len(rows)} 8-K filings from {len({r[0] for r in rows})} issuers")


def _get(url: str, agent: str) -> bytes | None:
    request = urllib.request.Request(url, headers={"User-Agent": agent, "Accept-Encoding": "gzip"})
    for attempt in range(6):
        with _gate:
            wait = _next_request[0] - time.monotonic()
            _next_request[0] = max(time.monotonic(), _next_request[0]) + REQUEST_INTERVAL
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                body = response.read()
                return gzip.decompress(body) if response.headers.get("Content-Encoding") == "gzip" else body
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            time.sleep(10 * (attempt + 1))                      # 403 and 429 are the SEC's rate limit
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"sec.gov kept failing for {url}")


def issuer_times(cik: str, agent: str) -> list[tuple[str, str, str]]:
    """(cik, accession, acceptance time in UTC) for the issuer's 8-K filings since late 2015 (PR #92's code)."""
    body = _get(f"https://data.sec.gov/submissions/CIK{cik}.json", agent)
    if body is None:
        return []
    main = json.loads(body)
    blocks = [main["filings"]["recent"]]
    for extra in main["filings"].get("files") or []:
        if (extra.get("filingTo") or "9999") >= FIRST_FILED:
            older = _get(f"https://data.sec.gov/submissions/{extra['name']}", agent)
            if older is not None:
                blocks.append(json.loads(older))
    return [(cik, accession, stamp) for block in blocks
            for form, accession, stamp, filed in zip(block["form"], block["accessionNumber"],
                                                     block["acceptanceDateTime"], block["filingDate"])
            if form == "8-K" and filed >= FIRST_FILED]


def acceptance(work: Path, earlier_csv: Path, earlier_done: Path) -> None:
    agent = os.environ["SEC_USER_AGENT"]
    target, done_path = work / "sec" / "acceptance.csv", work / "sec" / "acceptance.done"
    if not target.exists():
        shutil.copy(earlier_csv, target)
        shutil.copy(earlier_done, done_path)
    with (work / "sec" / "filings_8k.csv").open() as handle:
        wanted = sorted({row["cik"] for row in csv.DictReader(handle)})
    done = set(done_path.read_text().split())
    todo = [cik for cik in wanted if cik not in done]
    print(f"{len(wanted)} issuers with 8-K filings, {len(todo)} to fetch", flush=True)
    with target.open("a", newline="") as out, done_path.open("a") as marks, ThreadPoolExecutor(WORKERS) as pool:
        writer = csv.writer(out)
        for count, (cik, rows) in enumerate(zip(todo, pool.map(lambda c: issuer_times(c, agent), todo)), 1):
            writer.writerows(rows)
            marks.write(cik + "\n")
            if count % 500 == 0:
                out.flush(), marks.flush()
                print(f"{count} of {len(todo)} issuers", flush=True)
    print(f"{len(wanted)} issuers done")


if __name__ == "__main__":
    command, work_dir = sys.argv[1], Path(sys.argv[2])
    if command == "filings":
        filings(work_dir, Path(sys.argv[3]), Path(sys.argv[4]))
    elif command == "acceptance":
        acceptance(work_dir, Path(sys.argv[3]), Path(sys.argv[4]))
    else:
        raise SystemExit("commands: filings, acceptance")
