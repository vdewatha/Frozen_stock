"""Every Alpaca news item from 2016-01-01 to 2026-11-11: headline, summary, symbols. Nothing else.

    ALPACA_KEY=... ALPACA_SECRET=... python3 -I news_fetch.py <work_dir>

Reads GET https://data.alpaca.markets/v1beta1/news one UTC day at a time (50 items a page, the API's
maximum), under the free plan's 200 requests a minute. Each day's items go to
<work_dir>/news/<YYYY-MM>.jsonl.gz and the day is marked in <work_dir>/news/days.done only after its last
page, so a rerun repeats nothing and a half-read day is read again (items are de-duplicated by id on load).

Market data only: never an account or an order endpoint, never the deployed app. The keys come from the
environment and are never written or printed. No price is read here.
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

URL = "https://data.alpaca.markets/v1beta1/news"
FIRST_DAY = date(2016, 1, 1)
LAST_DAY = date(2026, 11, 11)
REQUEST_INTERVAL = 0.32           # about 187 requests a minute
WORKERS = 3
SUMMARY_CHARS = 1000

_gate = threading.Lock()
_next_request = [0.0]
_write = threading.Lock()


def _get(params: dict, key: str, secret: str) -> dict:
    request = urllib.request.Request(URL + "?" + urllib.parse.urlencode(params),
                                     headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    for attempt in range(8):
        with _gate:
            wait = _next_request[0] - time.monotonic()
            _next_request[0] = max(time.monotonic(), _next_request[0]) + REQUEST_INTERVAL
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("Alpaca news request kept failing")


def one_day(day: date, key: str, secret: str) -> list[dict]:
    items, token = [], None
    while True:
        params = {"start": f"{day}T00:00:00Z", "end": f"{day}T23:59:59Z", "limit": 50, "sort": "asc",
                  "include_content": "false"}
        if token:
            params["page_token"] = token
        body = _get(params, key, secret)
        for item in body.get("news") or []:
            items.append({"id": item.get("id"), "created_at": item.get("created_at"),
                          "headline": (item.get("headline") or "").strip(),
                          "summary": (item.get("summary") or "").strip()[:SUMMARY_CHARS],
                          "symbols": item.get("symbols") or [], "source": item.get("source") or "",
                          "author": item.get("author") or ""})
        token = body.get("next_page_token")
        if not token:
            return items


def main(work: Path) -> None:
    key, secret = os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"]
    target = work / "news"
    target.mkdir(parents=True, exist_ok=True)
    done_path = target / "days.done"
    done = set(done_path.read_text().split()) if done_path.exists() else set()
    days = [FIRST_DAY + timedelta(days=i) for i in range((LAST_DAY - FIRST_DAY).days + 1)]
    todo = [day for day in days if day.isoformat() not in done]
    print(f"{len(days)} days, {len(todo)} to fetch", flush=True)
    started, count, total = time.time(), 0, 0

    def work_day(day: date):
        items = one_day(day, key, secret)
        with _write:
            with gzip.open(target / f"{day.isoformat()[:7]}.jsonl.gz", "at", encoding="utf-8") as out:
                for item in items:
                    out.write(json.dumps(item, ensure_ascii=False) + "\n")
            with done_path.open("a") as marks:
                marks.write(day.isoformat() + "\n")
        return day, len(items)

    with ThreadPoolExecutor(WORKERS) as pool:
        for day, n in pool.map(work_day, todo):
            count, total = count + 1, total + n
            if count % 100 == 0 or count == len(todo):
                print(f"{count}/{len(todo)} days ({time.time() - started:.0f} s), {total} items; last {day}: {n}",
                      flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
