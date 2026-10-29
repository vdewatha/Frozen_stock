"""Compact audit payloads for high-volume market-data writers.

Minutely preflight / research collections used to store every per-symbol
result in the hash-chained audit log, which made each row several KB and the
chain slow to verify. They now store a summary: counts, the symbols that need
attention, and a sha256 of the canonical full (redacted) result so the full
evidence returned to the caller can still be matched against the audit row.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from typing import Any, Callable, Iterable

COMPACT_AUDIT_VERSION = 1
MAX_REASON_CHARS = 160


def canonical_sha256(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _rows_imported(row: dict) -> int:
    value = row.get("rows_imported")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def compact_symbol_results(
    results: Iterable[dict],
    *,
    needs_attention: Callable[[dict], bool],
    detail_keys: tuple[str, ...],
) -> dict:
    """Summarize per-symbol results; only rows needing attention keep details.

    ``results`` keeps its key name for existing readers, but now holds only
    the non-ready (or repair-pending) symbols; ``results_scope`` says so.
    """
    rows = [row for row in results if isinstance(row, dict)]
    attention = []
    for row in rows:
        if not needs_attention(row):
            continue
        item = {key: row.get(key) for key in detail_keys if row.get(key) is not None}
        reason = item.get("unavailable_reason")
        if isinstance(reason, str) and len(reason) > MAX_REASON_CHARS:
            item["unavailable_reason"] = reason[:MAX_REASON_CHARS]
        attention.append(item)
    return {
        "compact_audit_version": COMPACT_AUDIT_VERSION,
        "symbol_count": len(rows),
        "status_counts": dict(sorted(Counter(str(row.get("status")) for row in rows).items())),
        "rows_imported": sum(_rows_imported(row) for row in rows),
        "non_ready_count": len(attention),
        "results_scope": "non_ready_only",
        "results": attention,
    }


def compact_payload(full_payload: dict, *, needs_attention, detail_keys) -> dict:
    """Return ``full_payload`` with its per-symbol ``results`` summarized.

    ``full_result_sha256`` is the digest of the canonical full payload as it
    would have been stored before compaction.
    """
    summary = compact_symbol_results(
        full_payload.get("results") or [],
        needs_attention=needs_attention,
        detail_keys=detail_keys,
    )
    compact = {key: value for key, value in full_payload.items() if key != "results"}
    return {**compact, **summary, "full_result_sha256": canonical_sha256(full_payload)}
