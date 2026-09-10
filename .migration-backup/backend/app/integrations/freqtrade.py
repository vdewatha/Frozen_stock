"""Read-only Freqtrade REST adapter; no upstream code or order methods.

Contract checked against upstream commit 9f10e357a93c1dcf10c2a2b367659214d89c073e.
The base URL is the server origin (not /api/v1). HTTP is allowed only on
loopback; use HTTPS for remote services. Basic authentication is supported by
Freqtrade's http_basic_or_jwt_token dependency.
"""
from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlsplit

import httpx


class FreqtradeError(RuntimeError):
    """Sanitized integration failure, safe to display without credentials."""


class FreqtradeClient:
    def __init__(
        self, base_url: str, username: str, password: str, *,
        timeout: float = 5.0, transport: httpx.BaseTransport | None = None,
    ) -> None:
        try:
            url = urlsplit(base_url)
            valid = (
                url.scheme in {"http", "https"} and bool(url.hostname)
                and not url.username and not url.password
                and not url.query and not url.fragment and url.path in {"", "/"}
                and (url.scheme == "https" or url.hostname in {"localhost", "127.0.0.1", "::1"})
            )
            _ = url.port
        except ValueError:
            valid = False
        if not valid:
            raise FreqtradeError("Use an HTTPS server origin, or HTTP loopback, without URL credentials.")
        if not username or not password or ":" in username:
            raise FreqtradeError("A username and password are required; username cannot contain a colon.")
        if not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise FreqtradeError("Timeout must be greater than zero and at most 30 seconds.")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v1/",
            auth=httpx.BasicAuth(username, password), timeout=timeout,
            follow_redirects=False, trust_env=False, transport=transport,
        )

    def __enter__(self) -> FreqtradeClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, endpoint: str) -> Any:
        try:
            response = self._client.get(endpoint)
        except httpx.HTTPError:
            raise FreqtradeError("Freqtrade request failed or timed out.") from None
        if response.status_code != 200:
            raise FreqtradeError(f"Freqtrade returned HTTP {response.status_code}.")
        try:
            return response.json()
        except (ValueError, UnicodeError):
            raise FreqtradeError("Freqtrade returned invalid JSON.") from None

    def ping(self) -> bool:
        data = self._get("ping")
        if not isinstance(data, dict) or data.get("status") != "pong":
            raise FreqtradeError("Freqtrade ping response is invalid.")
        return True

    def show_config(self) -> dict[str, Any]:
        """Return an allowlisted summary only after explicit dry-run verification."""
        data = self._get("show_config")
        if not isinstance(data, dict) or data.get("dry_run") is not True:
            raise FreqtradeError("Freqtrade must explicitly report dry_run=true.")
        # Never return arbitrary upstream config fields (including credentials).
        keys = ("version", "api_version", "dry_run", "exchange", "timeframe", "state", "runmode")
        return {key: data[key] for key in keys if key in data}

    def status(self) -> dict[str, Any]:
        """Inspect dry-run positions; verify mode on every call, never cache it."""
        config = self.show_config()
        trades = self._get("status")
        if not isinstance(trades, list) or any(not isinstance(trade, dict) for trade in trades):
            raise FreqtradeError("Freqtrade status response is invalid.")
        return {"config": config, "open_trade_count": len(trades)}
