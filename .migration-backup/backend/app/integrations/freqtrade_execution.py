"""Pinned, explicitly dry-run-only Freqtrade order transport.

Contract: Freqtrade 2026.8 api_schemas ForceEnterPayload/ForceExitPayload and
OrderSchema. An HTTP failure after POST is ambiguous, never a safe retry.
"""
import httpx
from app.integrations.freqtrade import FreqtradeClient, FreqtradeError


class FreqtradeDryRunClient(FreqtradeClient):
    def verify(self):
        config = self._get("show_config")
        expected = {"dry_run": True, "exchange": "kraken", "timeframe": "1h",
                    "trading_mode": "spot", "version": "2026.8", "force_entry_enable": True,
                    "bot_name": "kraken-paper-execution", "strategy": "ObservationOnly",
                    "position_adjustment_enable": False, "short_allowed": False}
        if not isinstance(config, dict) or any(config.get(k) != v or (type(v) is bool and config.get(k) is not v) for k, v in expected.items()):
            raise FreqtradeError("Execution requires pinned Kraken spot hourly Freqtrade dry-run.")
        whitelist = self._get("whitelist")
        if not isinstance(whitelist, dict) or whitelist.get("whitelist") != ["BTC/USD"]:
            raise FreqtradeError("Execution requires a BTC/USD-only whitelist")
        return config

    def trades(self):
        self.verify()
        result = self._get("status")
        if not isinstance(result, list):
            raise FreqtradeError("Invalid dry-run trade list")
        return result

    def trade(self, trade_id):
        if type(trade_id) is not int or trade_id <= 0:
            raise FreqtradeError("Invalid dry-run trade identity")
        self.verify()
        result = self._get(f"trade/{trade_id}")
        if not isinstance(result, dict):
            raise FreqtradeError("Invalid dry-run trade")
        return result

    def history(self, *, max_trades=1000):
        """Read all bounded closed history plus open trades, never truncate silently.

        Upstream /trades contains CLOSED trades only, sorted by ascending id.
        A changing total or duplicate identity invalidates the scan.
        """
        if type(max_trades) is not int or not 1 <= max_trades <= 10000:
            raise FreqtradeError("Invalid history bound")
        self.verify()
        result, total, offset = [], None, 0
        while total is None or offset < total:
            page = self._get(f"trades?limit=100&offset={offset}&order_by_id=true")
            if (not isinstance(page, dict) or not isinstance(page.get("trades"), list)
                    or type(page.get("total_trades")) is not int
                    or not 0 <= page["total_trades"] <= max_trades
                    or page.get("offset") != offset
                    or page.get("trades_count") != len(page["trades"])
                    or len(page["trades"]) > 100
                    or (total is not None and total != page["total_trades"])):
                raise FreqtradeError("Incomplete or changing dry-run history")
            total = page["total_trades"]
            result.extend(page["trades"])
            offset = len(result)
            if offset > total or (offset < total and not page["trades"]):
                raise FreqtradeError("Incomplete dry-run history")
        result.extend(self.trades())
        ids = [t.get("trade_id") if isinstance(t, dict) else None for t in result]
        if any(type(i) is not int or i <= 0 for i in ids) or len(ids) != len(set(ids)):
            raise FreqtradeError("Ambiguous dry-run history identity")
        return result

    def _post(self, endpoint, payload):
        config = self.verify()
        if config.get("state") != "running":
            raise FreqtradeError("Dry-run bot must be running for submission")
        try:
            response = self._client.post(endpoint, json=payload)
            if response.status_code != 200:
                raise FreqtradeError("Ambiguous dry-run submission; reconcile without retry.")
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (httpx.HTTPError, ValueError):
            raise FreqtradeError("Ambiguous dry-run submission; reconcile without retry.") from None

    def enter(self, *, price, quantity, tag):
        return self._post("forceenter", {"pair": "BTC/USD", "side": "long", "price": float(price),
            "ordertype": "limit", "stakeamount": float(price * quantity), "entry_tag": tag})

    def exit(self, *, trade_id, price, quantity):
        return self._post("forceexit", {"tradeid": trade_id, "ordertype": "limit",
                                       "price": float(price), "amount": float(quantity)})
