"""Submission-time order pagination with an overlapping nanosecond boundary."""
import pandas as pd

PAGE_SIZE = 500
MAX_PAGES = 100


def _submitted(row, error):
    value = row.get("submitted_at")
    try:
        if not isinstance(value, str) or "T" not in value:
            raise ValueError()
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tzinfo is None:
            raise ValueError()
        return stamp.tz_convert("UTC").as_unit("ns")
    except (ValueError, TypeError, OverflowError):
        raise error("Alpaca order pagination requires precise timezone-aware submission timestamps") from None


def collect_orders(request, error, *, after=None):
    """Never substitute update/create times for the provider's submission cursor."""
    rows, seen, until = [], {}, None
    for _ in range(MAX_PAGES):
        params = {"status": "all", "nested": "false", "direction": "desc", "limit": str(PAGE_SIZE)}
        if after is not None:
            params["after"] = after
        if until is not None:
            params["until"] = until.isoformat()
        page = request("GET", "/v2/orders", params=params)
        if not isinstance(page, list) or len(page) > PAGE_SIZE:
            raise error("Alpaca orders response is invalid")
        page_ids = set()
        for row in page:
            ident = row.get("id") if isinstance(row, dict) else None
            if not isinstance(ident, str) or not ident.strip() or ident in page_ids:
                raise error("Alpaca order page has missing or duplicate identity")
            page_ids.add(ident)
            if ident in seen:
                if row != seen[ident]:
                    raise error("Alpaca order changed during pagination; restart reconciliation")
            else:
                seen[ident] = row
                rows.append(row)
        stamps = [_submitted(row, error) for row in page] if len(page) == PAGE_SIZE or until is not None else []
        if stamps != sorted(stamps, reverse=True):
            raise error("Alpaca order page is not sorted by submission time")
        if until is not None and any(stamp >= until for stamp in stamps):
            raise error("Alpaca order page ignored the exclusive submission cursor")
        if len(page) < PAGE_SIZE:
            return rows
        # Re-read the oldest timestamp, including any unseen orders tied there.
        # A full same-time page cannot be split safely by a timestamp cursor.
        try:
            next_until = min(stamps) + pd.Timedelta(1, unit="ns")
        except (ValueError, OverflowError):
            raise error("Alpaca order submission timestamp is out of range") from None
        if until is not None and next_until >= until:
            raise error("Alpaca order submission cursor did not advance; boundary requires review")
        until = next_until
    raise error("Alpaca order pagination exceeded safe page limit")
