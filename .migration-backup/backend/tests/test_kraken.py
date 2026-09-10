from datetime import datetime, timezone
import unittest

import httpx

from app.integrations.kraken import BTC_USD, KrakenError, KrakenPublicClient
from app.services.instruments import Instrument, AssetClass, Timeframe


def payload():
    return {"error": [], "result": {"XXBTZUSD": [
        [3600, "100", "110", "90", "105", "102", "0.12345678", 4],
        [7200, "105", "110", "90", "100", "102", "0.5", 3],
        [10800, "100", "110", "90", "105", "102", "0.25", 2],
    ], "last": 10800}}


class KrakenTests(unittest.TestCase):
    clock = datetime.fromtimestamp(11000, timezone.utc)

    def fetch(self, data, **kwargs):
        with KrakenPublicClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))) as client:
            return client.ohlc(as_of=self.clock, **kwargs)

    def test_canonical_closed_rows_request_and_fractional_volume(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=60&since=3600")
            self.assertEqual(request.method, "GET")
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, json=payload())
        with KrakenPublicClient(transport=httpx.MockTransport(handler)) as client:
            rows = client.ohlc(since=3600, as_of=self.clock)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].instrument, BTC_USD)
            self.assertEqual(rows[0].volume, .12345678)
            self.assertEqual(client.history_limit, 720)
            self.assertIn("cannot", client.history_warning)

    def test_final_row_always_excluded_even_when_stale(self):
        with KrakenPublicClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload()))) as client:
            self.assertEqual(len(client.ohlc(as_of=datetime.fromtimestamp(99999, timezone.utc))), 2)
        data = payload()
        data["result"]["XXBTZUSD"] = data["result"]["XXBTZUSD"][-1:]
        self.assertEqual(self.fetch(data), [])

    def test_malformed_rows_fail_closed(self):
        for column, value in [(0, True), (0, 3601), (1, "NaN"), (2, "99"), (3, "111"),
                              (4, "Infinity"), (5, "1000"), (6, "-1"), (7, -1), (7, True), (1, "1e999")]:
            data = payload()
            data["result"]["XXBTZUSD"][0][column] = value
            with self.subTest(column=column, value=value), self.assertRaises(KrakenError):
                self.fetch(data)
        for rows in [[], [payload()["result"]["XXBTZUSD"][0]] * 722,
                     [payload()["result"]["XXBTZUSD"][0]] * 2,
                     payload()["result"]["XXBTZUSD"][::2], [[3600]]]:
            data = payload()
            data["result"]["XXBTZUSD"] = rows
            with self.assertRaises(KrakenError):
                self.fetch(data)

    def test_observed_720_committed_plus_current_response(self):
        data = payload()
        template = data["result"]["XXBTZUSD"][0]
        data["result"]["XXBTZUSD"] = [[i * 3600, *template[1:]] for i in range(721)]
        clock = datetime.fromtimestamp(721 * 3600, timezone.utc)
        with KrakenPublicClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))) as client:
            self.assertEqual(len(client.ohlc(as_of=clock)), 720)

    def test_pair_cursor_payload_and_provider_errors(self):
        for data in [None, [], {}, {"error": ["secret"]}, {"error": [], "result": {}},
                     {"error": [], "result": {"BTCUSD": [], "last": 1}}]:
            with self.assertRaises(KrakenError) as caught:
                self.fetch(data)
            self.assertNotIn("secret", str(caught.exception))
        data = payload()
        data["result"]["last"] = True
        with self.assertRaises(KrakenError):
            self.fetch(data)

    def test_future_and_incomplete_committed_rows(self):
        for clock in [10000, 7000]:
            with KrakenPublicClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload()))) as client:
                with self.assertRaises(KrakenError):
                    client.ohlc(as_of=datetime.fromtimestamp(clock, timezone.utc))

    def test_invalid_arguments_no_network(self):
        def handler(request):
            self.fail("Network must not be called")
        with KrakenPublicClient(transport=httpx.MockTransport(handler)) as client:
            for kwargs in [{"instrument": Instrument(AssetClass.CRYPTO_SPOT, "OTHER", "BTC", "USD")},
                           {"timeframe": "1h"}, {"since": True}, {"since": -1}, {"as_of": datetime(2020, 1, 1)}]:
                with self.assertRaises(KrakenError):
                    client.ohlc(**kwargs)
        for timeout in [0, 31, True, float("nan"), "1"]:
            with self.assertRaises(KrakenError):
                KrakenPublicClient(timeout=timeout)

    def test_transport_http_redirect_and_json_failures_sanitized(self):
        def timeout(request):
            raise httpx.ReadTimeout("secret")
        handlers = [timeout, lambda r: httpx.Response(302, headers={"Location": "https://bad.example"}),
                    lambda r: httpx.Response(503, text="secret"), lambda r: httpx.Response(200, text="secret")]
        for handler in handlers:
            with KrakenPublicClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(KrakenError) as caught:
                    client.ohlc()
                self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
