"""Point the Demo Tenant's Tenant webhook endpoint at the Demo runner (#286), for the
provision wizard: `uv run python -m demo_runner.webhook_setup --base-url https://<api host>`.

It registers the endpoint at `<base-url>/demo-runner/webhooks`, or moves the existing one
there and rotates its secret, since the API shows a secret only once. Then it prints the
signing secret on stdout as `secret: whsec_...`, for the wizard to write into the VM's
.env as AGENTPAY_DEMO_RUNNER_WEBHOOK_SECRET without showing it. Everything else goes to
stderr. The tenant key comes from AGENTPAY_DEMO_TENANT_KEY and is never printed.
"""

import argparse
import sys
import uuid
from collections.abc import Sequence

import httpx
from pydantic import ValidationError

from core.settings import DemoRunnerSettings


class SetupFailed(Exception):
    """The API refused: says how, never with the key."""


def _post(client: httpx.Client, path: str, body: dict[str, str] | None) -> httpx.Response:
    headers = {"Idempotency-Key": f"webhook-setup-{uuid.uuid4().hex}"}
    try:
        response = client.post(path, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise SetupFailed(f"couldn't reach the API ({type(exc).__name__})") from None
    if not response.is_success:
        try:
            code = response.json().get("code", "")
        except ValueError:
            code = ""
        raise SetupFailed(f"{path} answered HTTP {response.status_code} {code}".rstrip())
    return response


def point_endpoint_at(client: httpx.Client, url: str) -> str:
    """Register the endpoint at `url`, or move the existing one there and rotate its
    secret. Returns the signing secret the API showed."""
    registered = _post(client, "/v1/webhook-endpoint", {"url": url})
    secret = registered.json().get("secret")
    if secret is None:  # an endpoint already existed: it kept its old secret
        secret = _post(client, "/v1/webhook-endpoint/rotate-secret", None).json().get("secret")
    if not secret:
        raise SetupFailed("the API answered without a secret")
    return str(secret)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m demo_runner.webhook_setup")
    parser.add_argument("--base-url", required=True, help="the hosted API, https://<api host>")
    args = parser.parse_args(argv)
    try:
        settings = DemoRunnerSettings()
    except ValidationError:
        print(
            "Set AGENTPAY_DEMO_AGENT_KEY and AGENTPAY_DEMO_TENANT_KEY (test keys).", file=sys.stderr
        )
        return 1
    base = args.base_url.rstrip("/")
    key = settings.demo_tenant_key.get_secret_value()
    with httpx.Client(
        base_url=base, headers={"Authorization": f"Bearer {key}"}, timeout=30.0
    ) as client:
        try:
            secret = point_endpoint_at(client, f"{base}/demo-runner/webhooks")
        except SetupFailed as failed:
            print(f"Webhook setup failed: {failed}", file=sys.stderr)
            return 1
    print(f"The Demo Tenant's webhooks now go to {base}/demo-runner/webhooks.", file=sys.stderr)
    print(f"secret: {secret}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
