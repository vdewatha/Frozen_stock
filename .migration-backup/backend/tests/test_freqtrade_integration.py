import base64
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx

from app.integrations.freqtrade import FreqtradeClient, FreqtradeError


class FreqtradeTests(unittest.TestCase):
    def client(self, handler):
        return FreqtradeClient("http://127.0.0.1:8080", "user", "secret", transport=httpx.MockTransport(handler))

    def test_authenticated_read_only_status(self):
        paths = []

        def handler(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["authorization"], "Basic " + base64.b64encode(b"user:secret").decode())
            self.assertEqual(request.extensions["timeout"]["read"], 5)
            paths.append(request.url.path)
            payload = {"ping": {"status": "pong"}, "show_config": {"dry_run": True, "version": "test", "password": "hidden"}, "status": [{"trade_id": 1}]}
            return httpx.Response(200, json=payload[request.url.path.rsplit("/", 1)[-1]])

        with self.client(handler) as client:
            self.assertTrue(client.ping())
            self.assertEqual(client.status(), {"config": {"dry_run": True, "version": "test"}, "open_trade_count": 1})
        self.assertEqual(paths, ["/api/v1/ping", "/api/v1/show_config", "/api/v1/status"])

    def test_live_missing_and_string_modes_fail_before_status(self):
        for value in (False, None, "true", 1):
            with self.subTest(value=value):
                requests = []
                def handler(request):
                    requests.append(request.url.path)
                    return httpx.Response(200, json={"dry_run": value})
                with self.client(handler) as client:
                    with self.assertRaises(FreqtradeError):
                        client.status()
                self.assertEqual(requests, ["/api/v1/show_config"])

    def test_mode_checked_on_every_status(self):
        modes = iter([True, False])
        def handler(request):
            return httpx.Response(200, json={"dry_run": next(modes)} if request.url.path.endswith("show_config") else [])
        with self.client(handler) as client:
            client.status()
            with self.assertRaises(FreqtradeError):
                client.status()

    def test_redirects_errors_and_malformed_payloads(self):
        for response in (httpx.Response(302, headers={"location": "https://evil.example"}), httpx.Response(401, text="secret"), httpx.Response(200, text="not json"), httpx.Response(200, json=[])):
            with self.subTest(response=response):
                with self.client(lambda request: response) as client:
                    with self.assertRaises(FreqtradeError) as error:
                        client.show_config()
                    self.assertNotIn("secret", str(error.exception))

    def test_timeout_sanitizes_exception(self):
        def handler(request):
            raise httpx.ReadTimeout("secret", request=request)
        with self.client(handler) as client:
            with self.assertRaises(FreqtradeError) as error:
                client.ping()
            self.assertNotIn("secret", str(error.exception))

    def test_bad_status(self):
        with self.client(lambda r: httpx.Response(200, json={"dry_run": True})) as client:
            with self.assertRaises(FreqtradeError):
                client.status()

    def test_configuration_validation(self):
        for url in ("http://example.com", "https://user:secret@example.com", "https://example.com/api/v1", "https://example.com?secret=x", "file:///tmp/test", "https://example.com:bad"):
            with self.subTest(url=url), self.assertRaises(FreqtradeError):
                FreqtradeClient(url, "user", "secret")
        for timeout in (0, -1, 31, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(FreqtradeError):
                FreqtradeClient("https://example.com", "user", "secret", timeout=timeout)
        with self.assertRaises(FreqtradeError):
            FreqtradeClient("https://example.com", "", "")

    def test_cli_environment_and_sanitized_failure(self):
        spec = importlib.util.spec_from_file_location("check_freqtrade", Path(__file__).parents[1] / "scripts" / "check_freqtrade.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        environment = {"FREQTRADE_URL": "http://127.0.0.1:8080", "FREQTRADE_USERNAME": "user", "FREQTRADE_PASSWORD": "private-secret"}
        with patch.dict("os.environ", environment, clear=True), patch.object(cli, "FreqtradeClient") as factory:
            factory.return_value.__enter__.return_value.status.return_value = {"config": {"dry_run": True}, "open_trade_count": 0}
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(), 0)
            self.assertTrue(json.loads(output.getvalue())["ok"])
            self.assertNotIn("private-secret", output.getvalue())
            factory.assert_called_once_with("http://127.0.0.1:8080", "user", "private-secret", timeout=5.0)
        with patch.dict("os.environ", {"FREQTRADE_TIMEOUT_SECONDS": "private-secret"}, clear=True):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(), 1)
            self.assertFalse(json.loads(output.getvalue())["ok"])
            self.assertNotIn("private-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
