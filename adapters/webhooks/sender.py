"""Posts Tenant webhooks over HTTPS with httpx. A short timeout and no redirects: a slow or
moved endpoint is a failed send, retried later. Nothing here logs."""

from collections.abc import Mapping

import httpx

from core.ports import WebhookSender, WebhookUnreachable

TIMEOUT_SECONDS = 5.0


class HttpWebhookSender:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=transport
        )

    def post(self, url: str, body: bytes, headers: Mapping[str, str]) -> int:
        try:
            return self._client.post(url, content=body, headers=dict(headers)).status_code
        except httpx.HTTPError as exc:
            raise WebhookUnreachable(type(exc).__name__) from None


_conforms: type[WebhookSender] = HttpWebhookSender  # mypy checks the adapter matches the port
