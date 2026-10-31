"""A thin client for Razorpay's REST API (test mode only, ADR-0012).

Every answer is one of three things:
- a JSON body;
- `RazorpayRefused`: a 4xx whose meaning is final (the request itself was refused);
- `RailUnavailable`: the outcome is unknown or not ours to judge (timeout, network, 5xx,
  429 rate limit, 401/403 bad keys), so the caller looks again later.

Nothing here logs: keys travel in the Authorization header, and bodies may carry
customer details, so neither may reach a log line.
"""

from typing import Any

import httpx
from pydantic import SecretStr

from core.ports import RailUnavailable

API = "https://api.razorpay.com/v1"
TIMEOUT_SECONDS = 10.0
_NOT_OURS_TO_JUDGE = frozenset({401, 403, 429})


class RazorpayRefused(Exception):
    """Razorpay refused the request. `code` is its error code (such as
    BAD_REQUEST_ERROR), `reason` its finer reason when it gives one."""

    def __init__(self, status: int, code: str, reason: str | None) -> None:
        super().__init__(f"HTTP {status} {code} ({reason})")
        self.status = status
        self.code = code
        self.reason = reason


class RazorpayApi:
    def __init__(
        self, key_id: str, key_secret: SecretStr, transport: httpx.BaseTransport | None = None
    ) -> None:
        if not key_id.startswith("rzp_test_"):
            raise ValueError("only a Razorpay test key (rzp_test_...) is accepted")
        self._client = httpx.Client(
            base_url=API,
            auth=(key_id, key_secret.get_secret_value()),
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )

    def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        return self._call("GET", path, params=params)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", path, json=body)

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:  # timeouts and network errors
            raise RailUnavailable(f"Razorpay {method} {path}: {type(exc).__name__}") from None
        status = response.status_code
        if status >= 500 or status in _NOT_OURS_TO_JUDGE:
            raise RailUnavailable(f"Razorpay {method} {path}: HTTP {status}")
        try:
            body: dict[str, Any] = response.json()
        except ValueError:
            raise RailUnavailable(f"Razorpay {method} {path}: HTTP {status}, not JSON") from None
        if status >= 400:
            found = body.get("error") if isinstance(body, dict) else None
            error: dict[str, Any] = found if isinstance(found, dict) else {}
            reason = error.get("reason")
            raise RazorpayRefused(
                status,
                str(error.get("code") or "UNKNOWN"),
                str(reason) if reason and reason != "NA" else None,
            )
        return body
