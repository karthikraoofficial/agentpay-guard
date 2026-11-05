"""`uv run python -m demo --base-url <api>`: run the demo journeys against a deployment.

The Demo Tenant's keys come from settings (AGENTPAY_DEMO_AGENT_KEY and
AGENTPAY_DEMO_TENANT_KEY), never from the command line, and are never printed. The
deployment must use the mock rail; journey 3 also needs mock settle on and a worker."""

import argparse
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from core.settings import Settings, get_settings
from demo.journeys import Demo, StepFailed

JOURNEYS = ("1", "2", "3")


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m demo", description=__doc__)
    parser.add_argument("--base-url", required=True, help="the API, e.g. https://api.example")
    parser.add_argument(
        "--journey", choices=(*JOURNEYS, "all"), default="all", help="one journey, or all three"
    )
    parser.add_argument("--interval", type=float, default=3.0, help="seconds between checks")
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for a step")
    args = parser.parse_args(argv)

    settings = settings or get_settings()
    if settings.demo_agent_key is None or settings.demo_tenant_key is None:
        print(
            "Set AGENTPAY_DEMO_AGENT_KEY and AGENTPAY_DEMO_TENANT_KEY (in .env) to the agent key "
            "and tenant key `python -m api.admin create-tenant` printed for the Demo Tenant.",
            file=sys.stderr,
        )
        return 2

    journeys = JOURNEYS if args.journey == "all" else (args.journey,)
    with httpx.Client(base_url=args.base_url, timeout=30.0) as client:
        demo = Demo(
            client=client,
            agent_key=settings.demo_agent_key.get_secret_value(),
            tenant_key=settings.demo_tenant_key.get_secret_value(),
            say=lambda line: print(line, flush=True),
            wait=lambda: time.sleep(args.interval),
            now=lambda: datetime.now(UTC),
            patience=max(1, int(args.timeout / args.interval)),
        )
        for number in journeys:
            try:
                getattr(demo, f"journey_{number}")()
            except StepFailed as failed:
                failure = f"Journey {number} failed at '{failed.step}': {failed.detail}"
                print(failure, file=sys.stderr)
                return 1
            print()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
