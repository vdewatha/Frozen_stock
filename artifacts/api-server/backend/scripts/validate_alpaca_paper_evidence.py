"""Read-only candidate check. Does not initialize/reconcile a ledger or select a venue."""
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.services.stock_paper_ledger import AlpacaPaperClient, StockPaperUnavailable
from scripts.alpaca_paper_contract import assess_cost_timestamp_contract, assess_documented_activity_schema
from app.services.alpaca_activity_v2 import replay


class ReadOnlyCandidate(AlpacaPaperClient):
    def _request_response(self, method, path, *, params=None, payload=None):
        if method != "GET" or path not in {
            "/v2/account", "/v2/positions", "/v2/orders", "/v2/account/activities"
        } or payload is not None:
            raise RuntimeError("Candidate validation permits evidence GETs only")
        return super()._request_response(method, path, params=params, payload=payload)


def numeric(value):
    try:
        return value is not None and Decimal(str(value)).is_finite()
    except (InvalidOperation, ValueError):
        return False


def main():
    report = {"observed_at": datetime.now(UTC).isoformat(), "read_only": True}
    samples = []
    # Separate clients replay broker history without any local ledger/cursor state.
    for _ in range(2):
        sample, raw = {}, {}
        client = ReadOnlyCandidate()
        for name in ("account", "positions", "orders", "fills"):
            try:
                value = getattr(client, name)()
                raw[name] = value
                if name == "account":
                    sample[name] = {
                        "valid_object": isinstance(value, dict),
                        "cash_numeric": numeric(value.get("cash")),
                        "equity_numeric": numeric(value.get("equity")),
                        "identity_present": bool(value.get("id")),
                        "updated_at_present": bool(value.get("updated_at")),
                    }
                else:
                    sample[name] = {"count": len(value)}
                    if name in ("orders", "fills"):
                        sample[name]["missing_id"] = sum(not r.get("id") for r in value)
                    if name == "fills":
                        fills = [r for r in value if r.get("activity_type") == "FILL"]
                        sample[name].update({
                            "fill_count": len(fills),
                            "missing_commission": sum(r.get("commission") is None for r in fills),
                            "missing_transaction_time": sum(not r.get("transaction_time") for r in fills),
                            "missing_order_link": sum(not r.get("order_id") for r in fills),
                            "nonfill_count": len(value) - len(fills),
                        })
            except StockPaperUnavailable as exc:
                # Adapter errors deliberately omit broker bodies and credentials.
                sample[name] = {"error": str(exc)}
        sample["cost_timestamp_contract"] = assess_cost_timestamp_contract(raw.get("fills"))
        sample["documented_provider_schema"] = assess_documented_activity_schema(raw.get("fills"))
        try:
            sample["activity_journal_v2"] = replay(raw.get("fills"))
        except ValueError:
            sample["activity_journal_v2"] = {"status": "unavailable_or_requires_review", "launch_authorized": False}
        samples.append((sample, raw))
    report["reads"] = [s[0] for s in samples]
    report["history_replay_equal"] = {
        key: samples[0][1][key] == samples[1][1][key]
        if key in samples[0][1] and key in samples[1][1] else None
        for key in ("orders", "fills")
    }
    report["qualification"] = "not_established_by_read_only_probe"
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
