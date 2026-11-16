"""Give a research data folder a manifest and an id, check one, or list the ones on this machine.

    python scripts/research_snapshot.py presets
    python scripts/research_snapshot.py build ~/.paper-training/dip-rules-full-market-20261112
    python scripts/research_snapshot.py build <dir> --name my-data --contract contract.json --source "Alpaca SIP"
    python scripts/research_snapshot.py verify <dir> [--full]
    python scripts/research_snapshot.py list [--root DIR]

``build`` hashes every file under the folder and writes ``SNAPSHOT.json``
beside them (``app/services/research_data.py`` explains the format).  The
snapshot id is what a study, a discovery run or an engine experiment records
as the data it used.  A folder named like one of the presets (the caches the
2026-11-11 to 11-14 studies wrote) takes that preset's contract; any other
folder needs ``--contract``, a JSON object with a status and a note for each
of the seven parts.  A manifest states what the data lacks as plainly as what
it holds.  Nothing here touches the app's database or a broker.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import research_data  # noqa: E402

NO_DELISTING_RETURN = ("no delisting return: a delisted stock's last bar is its last exchange trade; the shares may "
                       "have kept some value over the counter, or none")
SEC_IDENTITY = ("(CIK, ticker) pairs from SEC filings; bars are fetched by Alpaca as-of date, so a ticker later "
                "reused by another company does not supply its bars; no permanent security id")
SEC_UNIVERSE = ("no foreign private issuers or ADRs and no OTC stocks; the SEC ticker map ends 2026-09-30; price and "
                "dollar-volume filters are applied by each study, not by the cache")
AFTER_THE_FACT = "manifest written after the study ran: it identifies the cache as found, not as it was on the run date"

PRESETS = {
    "dip-rules-full-market-20261112": {
        "layout": "legacy:dip_rules_check.py",
        "sources": ["SEC insider transaction data sets and submissions (ticker map)",
                    "Alpaca SIP daily bars 2016-2026 by as-of date, raw and adjustment=all"],
        "notes": [AFTER_THE_FACT, "reports/dip-rules-full-market-20261112.md",
                  "also the price source of the public-information and stocks-in-play studies"],
        "contract": {
            "security_identity": {"status": "partial", "note": SEC_IDENTITY + "; a price check (insider price over raw "
                                  "close, median within 0.7 to 1.4) and duplicate-series removal guard it (identity.csv.gz)"},
            "symbol_changes": {"status": "partial", "note": "Alpaca's as-of lookup follows a company across renames; "
                               "there is no symbol-history table"},
            "daily_bars": {"status": "provided", "note": "bars/<asof>.pkl.gz: daily bars for about 8,700 US stocks, "
                           "delisted included, one request group per as-of date"},
            "corporate_actions": {"status": "partial", "note": "only through the raw and all-adjusted series; no action table"},
            "terminal_payouts": {"status": "absent", "note": NO_DELISTING_RETURN + "; bankruptcy is tagged from 8-K "
                                 "Item 1.03 and the study reports a -100% sensitivity"},
            "event_availability": {"status": "absent", "note": "no event table in this cache"},
            "coverage_exclusions": {"status": "partial", "note": SEC_UNIVERSE},
        },
    },
    "earnings-drift-delisted-20261111": {
        "layout": "legacy:earnings_drift_check.py",
        "sources": ["SEC Form 3/4/5 data sets and submissions.zip (ticker map)", "SEC live submissions API (acceptance times)",
                    "Alpaca SIP daily bars by as-of date, raw and adjustment=all"],
        "notes": [AFTER_THE_FACT, "reports/earnings-drift-delisted-20261111.md"],
        "contract": {
            "security_identity": {"status": "partial", "note": SEC_IDENTITY + " (ticker_map.parquet)"},
            "symbol_changes": {"status": "partial", "note": "Alpaca's as-of lookup follows a company across renames; "
                               "there is no symbol-history table"},
            "daily_bars": {"status": "provided", "note": "alpaca/<YYYY-MM>.parquet: daily bars with delisted stocks, "
                           "raw and all-adjusted"},
            "corporate_actions": {"status": "partial", "note": "only through the raw and all-adjusted series; no action table"},
            "terminal_payouts": {"status": "absent", "note": NO_DELISTING_RETURN},
            "event_availability": {"status": "provided", "note": "sec/acceptance.csv: EDGAR acceptance time in UTC from "
                                   "the live API (the bulk file mixes three clocks)"},
            "coverage_exclusions": {"status": "partial", "note": SEC_UNIVERSE},
        },
    },
    "public-information-20261114": {
        "layout": "legacy:pubinfo_lib.py",
        "sources": ["Alpaca news archive (v1beta1/news) 2015-2026", "SEC 8-K filings", "House and Senate trade disclosures",
                    "prices: the dip-rules-full-market-20261112 cache"],
        "notes": [AFTER_THE_FACT, "reports/public-information-tests-20261114.md"],
        "contract": {
            "security_identity": {"status": "partial", "note": SEC_IDENTITY + "; fiscal.ai names the wrong company for "
                                  "about 8.6% of news tickers"},
            "symbol_changes": {"status": "partial", "note": "as in the dip-rules cache this study prices from"},
            "daily_bars": {"status": "absent", "note": "holds no bars: prices are read from the "
                           "dip-rules-full-market-20261112 cache"},
            "corporate_actions": {"status": "absent", "note": "none here; see the dip-rules cache"},
            "terminal_payouts": {"status": "absent", "note": NO_DELISTING_RETURN},
            "event_availability": {"status": "partial", "note": "news per UTC publication day (news/<YYYY-MM>.jsonl.gz), "
                                   "8-K acceptance times (sec/), disclosure dates for members' trades (house/, senate/, "
                                   "congress/); each as its source records it"},
            "coverage_exclusions": {"status": "partial", "note": SEC_UNIVERSE + "; the news archive starts in 2015"},
        },
    },
    "stocks-in-play-breakout-20261112": {
        "layout": "legacy:stocks_in_play_orb.py",
        "sources": ["Alpaca SIP 1-minute and 5-minute bars", "daily bars: the dip-rules-full-market-20261112 cache"],
        "notes": [AFTER_THE_FACT, "reports/stocks-in-play-breakout-20261112.md"],
        "contract": {
            "security_identity": {"status": "partial", "note": "tickers as in the dip-rules cache; no permanent security id"},
            "symbol_changes": {"status": "partial", "note": "as in the dip-rules cache"},
            "daily_bars": {"status": "partial", "note": "minute bars only, for the top relative-volume stocks of each "
                           "session (minutes_top/<date>.pkl.gz) and a base set (minutes_base/); daily bars come from "
                           "the dip-rules cache"},
            "corporate_actions": {"status": "partial", "note": "splits are detected by a price ratio outside 0.8 to 1.25; "
                                  "no action table"},
            "terminal_payouts": {"status": "absent", "note": "intraday study: positions close the same day"},
            "event_availability": {"status": "absent", "note": "no event table"},
            "coverage_exclusions": {"status": "partial", "note": "minute bars exist only for the stocks selected each "
                                    "session; 1-minute bars cannot order an entry and a stop inside one bar"},
        },
    },
    "spy-intraday-momentum-20261112": {
        "layout": "legacy:spy_intraday_momentum.py",
        "sources": ["Alpaca SIP and IEX 1-minute SPY bars", "Alpaca daily SPY bars, raw and adjustment=all"],
        "notes": [AFTER_THE_FACT, "reports/spy-intraday-momentum-20261112.md"],
        "contract": {
            "security_identity": {"status": "provided", "note": "one instrument: SPY"},
            "symbol_changes": {"status": "provided", "note": "not applicable: SPY's ticker did not change"},
            "daily_bars": {"status": "provided", "note": "minute_sip/ and minute_iex/ by month, daily_raw and daily_all"},
            "corporate_actions": {"status": "partial", "note": "dividends only through the adjusted daily series"},
            "terminal_payouts": {"status": "provided", "note": "not applicable: SPY is listed"},
            "event_availability": {"status": "absent", "note": "no event table"},
            "coverage_exclusions": {"status": "provided", "note": "a single instrument by design"},
        },
    },
}


def build(args: argparse.Namespace) -> int:
    root = Path(args.directory).expanduser()
    preset = PRESETS.get(args.preset or root.name)
    if args.contract:
        contract = json.loads(Path(args.contract).read_text())
    elif preset:
        contract = preset["contract"]
    else:
        print(f"{root.name} is not a preset: pass --contract with a status and note for each of "
              f"{', '.join(research_data.PARTS)}", file=sys.stderr)
        return 2
    manifest = research_data.build_manifest(
        root, name=args.name or root.name, contract=contract,
        sources=args.source or (preset or {}).get("sources") or [],
        layout=args.layout or (preset or {}).get("layout") or research_data.CANONICAL_LAYOUT,
        notes=(preset or {}).get("notes"), created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    path = research_data.write_manifest(root, manifest)
    print(json.dumps({"written": str(path), **research_data.dataset_ref(root), "totals": manifest["totals"]}, indent=2))
    return 0


def verify(args: argparse.Namespace) -> int:
    result = research_data.verify(Path(args.directory).expanduser(), full=args.full)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def listing(args: argparse.Namespace) -> int:
    print(json.dumps(research_data.list_snapshots(Path(args.root).expanduser() if args.root else None), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("build", help="hash a folder and write its SNAPSHOT.json")
    make.add_argument("directory")
    make.add_argument("--name")
    make.add_argument("--preset", choices=sorted(PRESETS))
    make.add_argument("--contract", help="JSON file: a status and note per part")
    make.add_argument("--source", action="append")
    make.add_argument("--layout")
    make.set_defaults(run=build)
    check = commands.add_parser("verify", help="compare a folder with its manifest")
    check.add_argument("directory")
    check.add_argument("--full", action="store_true", help="re-hash every file, not just sizes")
    check.set_defaults(run=verify)
    show = commands.add_parser("list", help="snapshots under the snapshot root")
    show.add_argument("--root")
    show.set_defaults(run=listing)
    commands.add_parser("presets", help="names of the known caches").set_defaults(
        run=lambda _args: print("\n".join(sorted(PRESETS))) or 0)
    args = parser.parse_args(argv)
    try:
        return int(args.run(args))
    except research_data.SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
