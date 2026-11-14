"""Test 2 inputs: the House Clerk's periodic transaction reports, 2016 to 2026.

    python3 -I house_fetch.py index <work_dir>     # the yearly indexes -> house/ptr_index.csv
    python3 -I house_fetch.py pdfs  <work_dir>     # each electronic report's PDF -> house/pdf/<year>/<DocID>.pdf
    osascript -l JavaScript pdf_text.js <work_dir>/house/pdf/<year>     # PDF -> text beside it (macOS PDFKit)
    python3 -I house_fetch.py parse <work_dir>     # the texts -> house/transactions.csv

Sources: https://disclosures-clerk.house.gov/public_disc/financial-pdfs/<year>FD.zip (the index of every
filing) and https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/<year>/<DocID>.pdf. Public files, no
login, about two and a half requests a second. A report filed on paper is a scan (its DocID is not the
8-digit electronic form) and cannot be read: it is listed in the index with ``electronic = 0`` and skipped.
No price is read here.
"""
from __future__ import annotations

import csv
import io
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

BASE = "https://disclosures-clerk.house.gov/public_disc"
YEARS = range(2016, 2027)
AGENT = "research-script/1.0"
REQUEST_INTERVAL = 0.4
WORKERS = 2

_gate = threading.Lock()
_next_request = [0.0]


def _get(url: str) -> bytes | None:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    for attempt in range(6):
        with _gate:
            wait = _next_request[0] - time.monotonic()
            _next_request[0] = max(time.monotonic(), _next_request[0]) + REQUEST_INTERVAL
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            time.sleep(10 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"the House Clerk site kept failing for {url}")


def is_electronic(doc_id: str) -> bool:
    return len(doc_id) == 8 and doc_id.startswith("2")


def index(work: Path) -> None:
    target = work / "house"
    (target / "index").mkdir(parents=True, exist_ok=True)
    rows = []
    for year in YEARS:
        path = target / "index" / f"{year}FD.zip"
        if not path.exists():
            body = _get(f"{BASE}/financial-pdfs/{year}FD.zip")
            if body is None:
                print(f"{year}: no index")
                continue
            path.write_bytes(body)
        archive = zipfile.ZipFile(path)
        with archive.open(f"{year}FD.txt") as handle:
            for row in csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8-sig"), delimiter="\t"):
                if (row.get("FilingType") or "").strip() != "P":
                    continue
                doc_id = (row.get("DocID") or "").strip()
                filed = datetime.strptime(row["FilingDate"].strip(), "%m/%d/%Y").date().isoformat()
                rows.append({"year": year, "doc_id": doc_id, "filing_date": filed, "last": row.get("Last", "").strip(),
                             "first": row.get("First", "").strip(), "state_district": row.get("StateDst", "").strip(),
                             "electronic": int(is_electronic(doc_id))})
    rows.sort(key=lambda r: (r["filing_date"], r["doc_id"]))
    with (target / "ptr_index.csv").open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    by_year: dict = {}
    for row in rows:
        counts = by_year.setdefault(row["year"], [0, 0])
        counts[row["electronic"]] += 1
    print(f"{len(rows)} periodic transaction reports; by index year (paper, electronic): {by_year}")


def pdfs(work: Path) -> None:
    target = work / "house"
    with (target / "ptr_index.csv").open() as handle:
        wanted = [(row["year"], row["doc_id"]) for row in csv.DictReader(handle) if row["electronic"] == "1"]
    todo = []
    for year, doc_id in wanted:
        path = target / "pdf" / year / f"{doc_id}.pdf"
        if not (path.exists() and path.stat().st_size > 0) and not path.with_suffix(".missing").exists():
            todo.append((year, doc_id, path))
    print(f"{len(wanted)} electronic reports, {len(todo)} to fetch", flush=True)

    def one(item):
        year, doc_id, path = item
        path.parent.mkdir(parents=True, exist_ok=True)
        body = _get(f"{BASE}/ptr-pdfs/{year}/{doc_id}.pdf")
        if body is None or not body.startswith(b"%PDF"):
            path.with_suffix(".missing").write_text("")
            return 0
        path.write_bytes(body)
        return 1

    started, got = time.time(), 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for count, ok in enumerate(pool.map(one, todo), 1):
            got += ok
            if count % 500 == 0 or count == len(todo):
                print(f"{count}/{len(todo)} ({time.time() - started:.0f} s), {got} saved", flush=True)


# ------------------------------------------------------------------ parsing

# Two layouts, both read from PDFKit's text.
#
# * Reports since about 2019: the form's small-capital labels arrive as a capital followed by NUL characters
#   ("FILING STATUS: New" is "F S: New" once the NULs are dropped, "SUBHOLDING OF:" is "S O:", and so on), and
#   every asset carries a type code in brackets ([ST] = stock).
# * Older reports: the labels are spelled out, there is no type code, days are not zero-padded, and some
#   capital letters come out lower-case ("Apple Inc. (AAPL)" reads "apple Inc. (aaPL)", a sale's "S" can read
#   "s", and in 2018-2019 a type code can read "[sT]"). Tickers, types and codes are therefore matched without
#   regard to case and upper-cased.
#
# One transaction ends with: type, trade date, notification date, amount. The asset text (with the ticker in
# parentheses and, in the newer layout, the type code) comes before it, except that a long asset name pushes
# the type code, or the whole type-date-amount line, below the row's label lines. So the rows are anchored on
# the type-date-amount pattern and the text between two anchors is read for the ticker and the type code; a
# type code at the very start of that text belongs to the row before when that row has none.
ROW = re.compile(r"(?<![A-Za-z0-9(\[])(P|S \(partial\)|S|E)\s+(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}/\d{1,2}/\d{4})\s+"
                 r"(\$[\d,]+(?:\s*-\s*\$[\d,]+)?|Over\s+\$[\d,]+|Spouse/DC\s+Over\s+\$[\d,]+)", re.IGNORECASE)
TICKER = re.compile(r"\(([A-Za-z][A-Za-z0-9]{0,4}(?:[./][A-Za-z]{1,2})?)\)")
ASSET_TYPE = re.compile(r"\[([A-Za-z]{2})\]")
LEADING_TYPE = re.compile(r"\s*-?\s*\[([A-Za-z]{2})\]")   # "- [ST]": a code inside a wrapped amount
LABEL_LINE = re.compile(r"^(?:F S|S O|D|C|L|FILING STATUS|SUBHOLDING OF|DESCRIPTION|COMMENTS?|LOCATION): .*$",
                        re.MULTILINE | re.IGNORECASE)
OWNER = re.compile(r"(?:^|\n)\s*(SP|JT|DC)\s", re.IGNORECASE)


def _date(text: str) -> str | None:
    try:
        return datetime.strptime(text, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def parse_text(text: str) -> list[dict]:
    """The transaction rows of one report's text, in order. ``layout`` is ``coded`` when the report uses type codes."""
    text = text.replace("\x00", "")
    layout = "coded" if ASSET_TYPE.search(text) else "old"
    rows, cursor = [], 0
    for match in ROW.finditer(text):
        segment = LABEL_LINE.sub("", text[cursor:match.start()])
        lead = LEADING_TYPE.match(segment)
        if lead and rows and not rows[-1]["asset_type"]:
            rows[-1]["asset_type"] = lead.group(1).upper()
            segment = segment[lead.end():]
        tickers, kinds, owner = TICKER.findall(segment), ASSET_TYPE.findall(segment), OWNER.search(segment)
        kind = " ".join(match.group(1).upper().split())
        rows.append({"layout": layout, "owner": owner.group(1).upper() if owner else "",
                     "asset": " ".join(segment.split())[-120:],
                     "ticker": tickers[-1].upper().replace("/", ".") if tickers else "",
                     "asset_type": kinds[-1].upper() if kinds else "", "type": "S (partial)" if kind.startswith("S (") else kind,
                     "trade_date": _date(match.group(2)) or "", "notified": _date(match.group(3)) or "",
                     "amount": " ".join(match.group(4).split())})
        cursor = match.end()
    if rows and layout == "coded" and not rows[-1]["asset_type"]:    # the last row's code, wrapped below its amount
        lead = LEADING_TYPE.match(LABEL_LINE.sub("", text[cursor:cursor + 80]))
        if lead:
            rows[-1]["asset_type"] = lead.group(1).upper()
    return rows


NOT_A_STOCK = re.compile(r"\b(options?|calls?|puts?|bonds?|notes?|warrants?|treasur\w*)\b", re.IGNORECASE)


def is_stock_row(row: dict) -> bool:
    """A stock with a ticker: type code ST, or, in the older layout without codes, no option or bond wording."""
    if not row["ticker"]:
        return False
    if row["layout"] == "coded":
        return row["asset_type"] == "ST"
    return not NOT_A_STOCK.search(row["asset"])


def parse(work: Path) -> None:
    target = work / "house"
    with (target / "ptr_index.csv").open() as handle:
        reports = [row for row in csv.DictReader(handle) if row["electronic"] == "1"]
    out_rows, unreadable, no_rows, missing = [], 0, 0, 0
    for report in reports:
        text_path = target / "pdf" / report["year"] / f"{report['doc_id']}.txt"
        if not text_path.exists():
            missing += 1
            continue
        text = text_path.read_text(errors="replace")
        if len(text.strip()) < 200:
            unreadable += 1
            continue
        rows = parse_text(text)
        if not rows:
            no_rows += 1
        for position, row in enumerate(rows):
            out_rows.append({"doc_id": report["doc_id"], "filing_date": report["filing_date"],
                             "member": f"{report['first']} {report['last']}".strip(),
                             "state_district": report["state_district"], "row": position, **row})
    with (target / "transactions.csv").open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(out_rows[0]))
        writer.writeheader()
        writer.writerows(out_rows)
    stocks = [r for r in out_rows if is_stock_row(r)]
    layouts = {name: sum(r["layout"] == name for r in out_rows) for name in ("coded", "old")}
    print(f"{len(reports)} electronic reports: {missing} without text, {unreadable} unreadable, {no_rows} with no "
          f"row found; {len(out_rows)} rows ({layouts}), {len(stocks)} stock rows with a ticker "
          f"({sum(r['type'] == 'P' for r in stocks)} purchases)")


if __name__ == "__main__":
    command, work_dir = sys.argv[1], Path(sys.argv[2])
    {"index": index, "pdfs": pdfs, "parse": parse}[command](work_dir)
