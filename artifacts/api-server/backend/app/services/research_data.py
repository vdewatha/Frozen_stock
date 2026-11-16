"""One versioned interface to historical research data, and a record of which data an experiment used.

The research scripts and the app read history through different paths.  The
scripts fetch whole-market bars (delisted stocks included) into ad-hoc cache
folders that carried no manifest; the app replays yfinance bars for a current
list.  A result recorded only its ``data_end``, so nothing said exactly which
data it was computed on, and one pipeline could have better data than another
without anyone seeing it.

**A snapshot** is a folder with a ``SNAPSHOT.json`` manifest:

* ``snapshot_id``: SHA-256 over the manifest's content (its name, layout,
  sources, contract and every file's path, size and hash).  Two folders with
  the same id hold the same bytes under the same stated meaning;
* ``contract``: for each of the seven things historical research depends on
  (``PARTS``) whether the snapshot provides it, provides part of it, or does
  not, and a note saying how.  A snapshot never claims what it lacks: missing
  delisting returns are ``absent``, and ``Snapshot.terminal`` then reports the
  payout as unknown with the brackets a sensitivity run should use;
* ``files``: path, bytes and SHA-256 of every file, so ``verify`` can tell a
  changed, missing or extra file.

**The interface** (``open_snapshot`` -> ``Snapshot``) reads the canonical
layout (``write_canonical`` writes it): security identity that survives
ticker changes and reuse, symbol history, raw daily bars with an adjusted
close, corporate actions, terminal information, events with the time they
became available, and coverage exclusions.  A part the snapshot does not
provide raises ``NotProvided`` with the contract's note rather than returning
something empty that reads as "none".  ``Snapshot.loader`` adapts a snapshot
to the frames the app's replays take, so discovery and the experiment engine
can run on the same data as a research script.

A cache written before this existed keeps its own layout
(``layout: "legacy:<script>"``): ``scripts/research_snapshot.py`` gives it a
manifest and an id now, and its files are still read by the script that
wrote them.

**The record.**  ``dataset_ref`` is what an experiment stores: the snapshot's
id and manifest hash, or, for the app's own yfinance path, a reference that
says so and names its known gaps (``live_path_ref``).  Strategy discovery and
the experiment engine record it on every run; the hypothesis register shows
it for each study.

Research data only.  Nothing here reaches a broker or an order.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

import pandas as pd

from app.core.config import settings

logger = logging.getLogger(__name__)
FORMAT = "research-snapshot-v1"
MANIFEST_NAME = "SNAPSHOT.json"
CANONICAL_LAYOUT = "canonical-v1"
PARTS = ("security_identity", "symbol_changes", "daily_bars", "corporate_actions", "terminal_payouts",
         "event_availability", "coverage_exclusions")
STATUSES = ("provided", "partial", "absent")
# What a study should assume a delisted stock's holder finally received when
# the snapshot does not know: the last close, half of it, or nothing.
TERMINAL_SENSITIVITY = (0.0, -0.5, -1.0)
BAR_COLUMNS = ["date", "open", "high", "low", "close", "volume", "adj_close"]
IGNORED = frozenset({MANIFEST_NAME, ".DS_Store"})


class SnapshotError(ValueError):
    """A manifest or snapshot that cannot be trusted as written."""


class NotProvided(LookupError):
    """The snapshot's contract says it does not hold what was asked for."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*")
                  if path.is_file() and path.name not in IGNORED and not path.name.endswith(".tmp"))


def check_contract(contract: Mapping) -> dict:
    """Every part present with a known status and a note; raises ``SnapshotError`` otherwise."""
    out = {}
    for part in PARTS:
        entry = contract.get(part)
        if not isinstance(entry, Mapping) or entry.get("status") not in STATUSES:
            raise SnapshotError(f"contract part {part!r} needs a status of {', '.join(STATUSES)}")
        note = str(entry.get("note") or "").strip()
        if not note:
            raise SnapshotError(f"contract part {part!r} needs a note saying how it is (or is not) provided")
        out[part] = {"status": entry["status"], "note": note}
    unknown = sorted(set(contract) - set(PARTS))
    if unknown:
        raise SnapshotError(f"unknown contract part(s): {', '.join(unknown)}")
    return out


def _identity(manifest: Mapping) -> dict:
    return {"format": manifest["format"], "name": manifest["name"], "layout": manifest["layout"],
            "sources": manifest["sources"], "contract": manifest["contract"],
            "files": [[item["path"], item["bytes"], item["sha256"]] for item in manifest["files"]]}


def build_manifest(root: Path, *, name: str, contract: Mapping, sources: Iterable[str],
                   layout: str = CANONICAL_LAYOUT, notes: Optional[Iterable[str]] = None,
                   created_at: Optional[str] = None) -> dict:
    """Hash every file under ``root`` and state what the snapshot provides.  Writes nothing."""
    root = Path(root)
    if not root.is_dir():
        raise SnapshotError(f"{root} is not a folder")
    files = [{"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": _file_sha256(path)}
             for path in _files(root)]
    if not files:
        raise SnapshotError(f"{root} holds no data file")
    manifest = {"format": FORMAT, "name": str(name), "layout": str(layout), "sources": [str(s) for s in sources],
                "contract": check_contract(contract), "files": files}
    manifest["snapshot_id"] = hashlib.sha256(_canonical(_identity(manifest))).hexdigest()
    manifest["totals"] = {"files": len(files), "bytes": sum(item["bytes"] for item in files)}
    manifest["notes"] = [str(note) for note in notes or []]
    manifest["created_at"] = created_at  # informational; not part of the id
    return manifest


def write_manifest(root: Path, manifest: Mapping) -> Path:
    path = Path(root) / MANIFEST_NAME
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    os.replace(temporary, path)
    return path


def load_manifest(root: Path) -> dict:
    """The folder's manifest, with its id recomputed and checked against what it states."""
    path = Path(root) / MANIFEST_NAME
    if not path.is_file():
        raise SnapshotError(f"{root} has no {MANIFEST_NAME}")
    manifest = json.loads(path.read_text())
    if manifest.get("format") != FORMAT:
        raise SnapshotError(f"{path} is not a {FORMAT} manifest")
    check_contract(manifest.get("contract") or {})
    if hashlib.sha256(_canonical(_identity(manifest))).hexdigest() != manifest.get("snapshot_id"):
        raise SnapshotError(f"{path}: the snapshot id does not match the manifest's content")
    return manifest


def manifest_sha256(root: Path) -> str:
    return _file_sha256(Path(root) / MANIFEST_NAME)


def verify(root: Path, *, full: bool = False) -> dict:
    """Compare the folder with its manifest: sizes always, hashes with ``full``."""
    root = Path(root)
    manifest = load_manifest(root)
    listed = {item["path"]: item for item in manifest["files"]}
    present = {path.relative_to(root).as_posix(): path for path in _files(root)}
    missing = sorted(set(listed) - set(present))
    extra = sorted(set(present) - set(listed))
    changed = []
    for name in sorted(set(listed) & set(present)):
        if present[name].stat().st_size != listed[name]["bytes"] or (
                full and _file_sha256(present[name]) != listed[name]["sha256"]):
            changed.append(name)
    return {"snapshot_id": manifest["snapshot_id"], "ok": not (missing or extra or changed), "missing": missing,
            "extra": extra, "changed": changed, "hashes_checked": bool(full), "files": len(listed)}


def dataset_ref(root: Path) -> dict:
    """What an experiment records about the data it used."""
    manifest = load_manifest(root)
    return {"kind": "snapshot", "snapshot_id": manifest["snapshot_id"], "manifest_sha256": manifest_sha256(root),
            "name": manifest["name"], "layout": manifest["layout"],
            "parts": {part: manifest["contract"][part]["status"] for part in PARTS}}


def live_path_ref(*, provider: str = "yfinance", universe: Optional[Mapping] = None,
                  data_end: Optional[str] = None) -> dict:
    """The reference recorded when a replay used the app's own cached history instead of a snapshot."""
    return {
        "kind": "live_path", "snapshot_id": None, "manifest_sha256": None, "name": f"app_{provider}_history",
        "layout": "app-cache", "provider": provider, "data_end": data_end, "universe": dict(universe or {}),
        "parts": {"security_identity": "absent", "symbol_changes": "absent", "daily_bars": "partial",
                  "corporate_actions": "partial", "terminal_payouts": "absent", "event_availability": "partial",
                  "coverage_exclusions": "partial"},
        "known_gaps": ["a ticker is its current instrument: a reused symbol serves another company's bars",
                       "delisted names are mostly missing, so the sample is survivorship-biased",
                       "prices are adjusted with today's factors; no raw prices are kept",
                       "a trade still open when the data ends is dropped; there is no delisting return"],
    }


# --------------------------------------------------------------- the registry

def snapshots_root() -> Path:
    configured = str(settings.research_snapshot_root or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".paper-training"


def list_snapshots(root: Optional[Path] = None) -> list[dict]:
    """Every folder under the snapshot root that carries a readable manifest."""
    root = Path(root) if root else snapshots_root()
    out = []
    if not root.is_dir():
        return out
    for path in sorted(root.glob(f"*/{MANIFEST_NAME}")):
        try:
            out.append({**dataset_ref(path.parent), "path": str(path.parent)})
        except (SnapshotError, OSError, ValueError) as exc:
            logger.warning("research data: %s is not a usable snapshot: %s", path.parent, exc)
    return out


def find_snapshot(snapshot_id: str, root: Optional[Path] = None) -> Optional[Path]:
    for item in list_snapshots(root):
        if item["snapshot_id"] == snapshot_id or item["snapshot_id"].startswith(snapshot_id) and len(snapshot_id) >= 12:
            return Path(item["path"])
    return None


# -------------------------------------------------------------- the interface

class Snapshot:
    """Read access to one canonical snapshot.  Each accessor honours the contract."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.manifest = load_manifest(self.root)
        self.snapshot_id: str = self.manifest["snapshot_id"]
        self._cache: dict[str, pd.DataFrame] = {}

    def ref(self) -> dict:
        return dataset_ref(self.root)

    def status(self, part: str) -> str:
        return self.manifest["contract"][part]["status"]

    def require(self, part: str) -> None:
        entry = self.manifest["contract"][part]
        if entry["status"] == "absent":
            raise NotProvided(f"{self.manifest['name']} does not provide {part.replace('_', ' ')}: {entry['note']}")
        if self.manifest["layout"] != CANONICAL_LAYOUT:
            raise NotProvided(f"{self.manifest['name']} is in the {self.manifest['layout']} layout: read its files "
                              f"with the script that wrote them")

    def _table(self, name: str) -> pd.DataFrame:
        if name not in self._cache:
            path = self.root / name
            if not path.is_file():
                raise NotProvided(f"{self.manifest['name']} has no {name}")
            self._cache[name] = pd.read_csv(path, dtype=str, keep_default_na=False)
        return self._cache[name]

    def securities(self) -> pd.DataFrame:
        self.require("security_identity")
        return self._table("securities.csv").copy()

    def symbol_history(self, security_id: Optional[str] = None) -> pd.DataFrame:
        self.require("symbol_changes")
        table = self._table("symbol_history.csv")
        return (table if security_id is None else table[table["security_id"] == str(security_id)]).copy()

    def resolve(self, ticker: str, on: Optional[str] = None) -> Optional[str]:
        """The security that held ``ticker`` on ``on`` (ISO date), or holds it last when no date is given."""
        self.require("security_identity")
        ticker = str(ticker).upper()
        if self.status("symbol_changes") == "absent":
            rows = self._table("securities.csv")
            match = rows[rows["ticker"].str.upper() == ticker] if "ticker" in rows.columns else rows.iloc[0:0]
            return match.iloc[-1]["security_id"] if len(match) else None
        table = self._table("symbol_history.csv")
        rows = table[table["ticker"].str.upper() == ticker]
        if on is not None:
            rows = rows[(rows["valid_from"] <= on) & ((rows["valid_to"] == "") | (rows["valid_to"] >= on))]
        if not len(rows):
            return None
        return rows.sort_values("valid_from").iloc[-1]["security_id"]

    def bars(self, security_id: str, *, adjusted: bool = True) -> pd.DataFrame:
        """Daily bars of one security: raw OHLC, or OHLC scaled by adjusted close over close."""
        self.require("daily_bars")
        for suffix in (".csv.gz", ".csv"):
            path = self.root / "bars" / f"{security_id}{suffix}"
            if path.is_file():
                break
        else:
            raise NotProvided(f"{self.manifest['name']} has no bars for {security_id}")
        frame = pd.read_csv(path)
        for column in ("open", "high", "low", "close", "volume", "adj_close"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if adjusted:
            factor = frame["adj_close"] / frame["close"]
            for column in ("open", "high", "low", "close"):
                frame[column] = frame[column] * factor
        return frame[BAR_COLUMNS].copy()

    def corporate_actions(self, security_id: Optional[str] = None) -> pd.DataFrame:
        self.require("corporate_actions")
        table = self._table("corporate_actions.csv")
        return (table if security_id is None else table[table["security_id"] == str(security_id)]).copy()

    def terminal(self, security_id: str) -> dict:
        """How the security's history ends.  An unknown payout is reported as unknown, with brackets, never as zero."""
        self.require("security_identity")
        rows = self._table("securities.csv")
        match = rows[rows["security_id"] == str(security_id)]
        if not len(match):
            raise NotProvided(f"{self.manifest['name']} has no security {security_id}")
        row = match.iloc[0]
        kind = row.get("terminal_kind") or "unknown"
        raw = str(row.get("terminal_return") or "").strip()
        if kind == "active":
            return {"security_id": str(security_id), "kind": kind, "last_bar": row.get("last_bar") or None,
                    "terminal_return": None, "known": True, "sensitivity": None,
                    "note": "still listed at the snapshot's end"}
        known = raw != "" and self.status("terminal_payouts") != "absent"
        return {"security_id": str(security_id), "kind": kind, "last_bar": row.get("last_bar") or None,
                "terminal_return": float(raw) if known else None, "known": known,
                "sensitivity": None if known else list(TERMINAL_SENSITIVITY),
                "note": ("recorded in the snapshot" if known else
                         "unknown: " + self.manifest["contract"]["terminal_payouts"]["note"])}

    def events(self, *, security_id: Optional[str] = None, event_type: Optional[str] = None) -> pd.DataFrame:
        """Events with ``available_at`` (UTC): the earliest time a strategy could have known."""
        self.require("event_availability")
        table = self._table("events.csv")
        if security_id is not None:
            table = table[table["security_id"] == str(security_id)]
        if event_type is not None:
            table = table[table["event_type"] == event_type]
        return table.sort_values("available_at").copy()

    def exclusions(self) -> pd.DataFrame:
        self.require("coverage_exclusions")
        return self._table("exclusions.csv").copy()

    def loader(self, *, on: Optional[str] = None) -> Callable[[str], pd.DataFrame]:
        """A ``loader(symbol)`` for the app's replays (``strategy_discovery.load_frames``): adjusted daily bars."""
        def load(symbol: str) -> pd.DataFrame:
            security_id = self.resolve(symbol, on=on)
            if security_id is None:
                raise NotProvided(f"{self.manifest['name']} has no security for {symbol}")
            frame = self.bars(security_id, adjusted=True)
            return frame[["date", "open", "high", "low", "close", "volume"]]
        return load


def open_snapshot(reference: str | Path, root: Optional[Path] = None) -> Snapshot:
    """Open a snapshot by folder or by id (at least 12 characters of it)."""
    path = Path(str(reference)).expanduser()
    if not (path.is_dir() and (path / MANIFEST_NAME).is_file()):
        found = find_snapshot(str(reference), root)
        if found is None:
            raise SnapshotError(f"no snapshot {reference!r} under {root or snapshots_root()}")
        path = found
    return Snapshot(path)


def _status(provided: bool, complete: bool = True) -> str:
    return "absent" if not provided else ("provided" if complete else "partial")


def write_canonical(root: Path, *, name: str, sources: Iterable[str], securities: pd.DataFrame,
                    bars: Mapping[str, pd.DataFrame], symbol_history: Optional[pd.DataFrame] = None,
                    corporate_actions: Optional[pd.DataFrame] = None, events: Optional[pd.DataFrame] = None,
                    exclusions: Optional[pd.DataFrame] = None, notes: Optional[Mapping[str, str]] = None,
                    created_at: Optional[str] = None) -> dict:
    """Write a canonical snapshot and its manifest; the contract is derived from what was given, never assumed."""
    root = Path(root)
    (root / "bars").mkdir(parents=True, exist_ok=True)
    notes = dict(notes or {})
    required = {"security_id", "terminal_kind"}
    if not required <= set(securities.columns):
        raise SnapshotError("securities needs security_id and terminal_kind columns")
    securities = securities.copy()
    if "terminal_return" not in securities.columns:
        securities["terminal_return"] = ""
    securities = securities.fillna("").astype(str)
    securities.to_csv(root / "securities.csv", index=False)
    for security_id, frame in bars.items():
        missing = [column for column in BAR_COLUMNS if column not in frame.columns]
        if missing:
            raise SnapshotError(f"bars for {security_id} lack {', '.join(missing)}")
        frame[BAR_COLUMNS].to_csv(root / "bars" / f"{security_id}.csv.gz", index=False, compression={
            "method": "gzip", "mtime": 0})  # no timestamp in the file, so the same bars hash the same
    for filename, table in (("symbol_history.csv", symbol_history), ("corporate_actions.csv", corporate_actions),
                            ("events.csv", events), ("exclusions.csv", exclusions)):
        if table is not None:
            table.fillna("").astype(str).to_csv(root / filename, index=False)
    ended = securities[securities["terminal_kind"] != "active"]
    known = ended[ended["terminal_return"].str.strip() != ""]
    have_bars = set(str(key) for key in bars)

    def part(key: str, status: str, default: str) -> dict:
        return {"status": status, "note": notes.get(key) or default}

    contract = {
        "security_identity": part("security_identity", "provided",
                                  "one security_id per company-instrument; tickers are attributes, not keys"),
        "symbol_changes": part("symbol_changes", _status(symbol_history is not None),
                               "symbol_history.csv: ticker per security with valid_from / valid_to"
                               if symbol_history is not None else "no symbol history: tickers are as of the snapshot"),
        "daily_bars": part("daily_bars", _status(bool(bars), have_bars >= set(securities["security_id"])),
                           "raw OHLCV with an adjusted close per security"),
        "corporate_actions": part("corporate_actions", _status(corporate_actions is not None),
                                  "corporate_actions.csv: splits and dividends by date" if corporate_actions is not None
                                  else "no action table: only the adjusted close reflects splits and dividends"),
        "terminal_payouts": part("terminal_payouts", _status(len(known) > 0, len(known) == len(ended)),
                                 "terminal_return recorded for delisted securities" if len(known) else
                                 "no delisting return: a delisted security's last bar is its last exchange trade"),
        "event_availability": part("event_availability", _status(events is not None),
                                   "events.csv: available_at is the earliest UTC time the event could be acted on"
                                   if events is not None else "no event table"),
        "coverage_exclusions": part("coverage_exclusions", _status(exclusions is not None),
                                    "exclusions.csv: what the universe leaves out and why" if exclusions is not None
                                    else "exclusions are not listed"),
    }
    manifest = build_manifest(root, name=name, contract=contract, sources=sources, layout=CANONICAL_LAYOUT,
                              created_at=created_at)
    write_manifest(root, manifest)
    return manifest


# ----------------------------------------------------------- the app's replays

def replay_dataset(*, injected: bool = False) -> tuple[dict, Optional[Callable[[str], pd.DataFrame]]]:
    """Which data a replay uses and how to load it: ``(reference, loader or None)``.

    With ``research_snapshot_id`` set (a local research run) the replay reads
    that snapshot and records its id.  Otherwise, and whenever the caller
    injects its own loader, the app's cached history is used and the
    reference says so.  A snapshot that is configured but cannot be opened is
    an error, not a silent fall back to other data.
    """
    configured = str(settings.research_snapshot_id or "").strip()
    if injected or not configured:
        return live_path_ref(), None
    snapshot = open_snapshot(configured)
    return snapshot.ref(), snapshot.loader()
