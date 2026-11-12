"""The only code of this test that talks to sec.gov: acceptance times for the Item 2.02 filings.

    SEC_USER_AGENT="<who you are> <contact email>" python -I sec_fetch.py submissions <work_dir>
    SEC_USER_AGENT="<who you are> <contact email>" python -I sec_fetch.py check <work_dir> [sample]

``submissions`` reads <work_dir>/filings.csv (from ``earnings_drift_check.py filings``) and asks the live
submissions API (https://data.sec.gov/submissions/CIK##########.json and its older-filings files) for each
issuer's 8-K acceptance times, which it serves in UTC. They go to <work_dir>/sec/acceptance.csv; a rerun
skips issuers already done. ``check`` draws a fixed random sample of events from <work_dir>/events_raw.csv
and compares each derived Eastern time with the "Accepted" line of the filing's own EDGAR page.

Neither command reads a price or a return. The SEC refuses scripts that do not declare a contact, so the
User-Agent comes from the environment and is not stored here. Requests stay at about five a second, under
the SEC's limit of ten.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

REQUEST_INTERVAL = 0.2
WORKERS = 4
FIRST_FILED = "2015-12-25"
SEED = 20261111

_gate = threading.Lock()
_next_request = [0.0]


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
    """(cik, accession, acceptance time in UTC) for the issuer's 8-K filings since late 2015."""
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


def submissions(work: Path) -> None:
    agent = os.environ["SEC_USER_AGENT"]
    wanted = sorted(set(pd.read_csv(work / "filings.csv", dtype=str)["cik"]))
    target, done_path = work / "sec" / "acceptance.csv", work / "sec" / "acceptance.done"
    done = set(done_path.read_text().split()) if done_path.exists() else set()
    todo = [cik for cik in wanted if cik not in done]
    fresh = not target.exists()
    with target.open("a", newline="") as out, done_path.open("a") as marks, ThreadPoolExecutor(WORKERS) as pool:
        writer = csv.writer(out)
        if fresh:
            writer.writerow(["cik", "accession", "accepted_utc"])
        for count, (cik, rows) in enumerate(zip(todo, pool.map(lambda c: issuer_times(c, agent), todo)), 1):
            writer.writerows(rows)
            marks.write(cik + "\n")
            if count % 500 == 0:
                out.flush(), marks.flush()
                print(f"{count} of {len(todo)} issuers", flush=True)
    print(f"{len(wanted)} issuers done")


def check(work: Path, sample: int) -> None:
    agent = os.environ["SEC_USER_AGENT"]
    events = pd.read_csv(work / "events_raw.csv", dtype=str).sample(sample, random_state=SEED)
    rows = []
    for event in events.to_dict("records"):
        accession = event["accession"]
        page = _get(f"https://www.sec.gov/Archives/edgar/data/{int(event['cik'])}/{accession.replace('-', '')}/"
                    f"{accession}-index.htm", agent)
        match = re.search(r"Accepted</div>\s*<div class=\"info\">([0-9-]{10}) ([0-9:]{8})", (page or b"").decode("utf-8", "replace"))
        shown = f"{match.group(1)}T{match.group(2)}" if match else None
        rows.append({"accession": accession, "derived": event["accepted"], "edgar_page": shown,
                     "match": shown == event["accepted"]})
    table = pd.DataFrame(rows)
    table.to_csv(work / "acceptance_time_check.csv", index=False)
    print(f"{int(table['match'].sum())} of {len(table)} sampled filings match EDGAR's own page")
    if not table["match"].all():
        print(table[~table["match"]].to_string())


if __name__ == "__main__":
    command, work_dir = sys.argv[1], Path(sys.argv[2])
    if command == "submissions":
        submissions(work_dir)
    elif command == "check":
        check(work_dir, int(sys.argv[3]) if len(sys.argv) > 3 else 200)
    else:
        raise SystemExit("commands: submissions, check")
