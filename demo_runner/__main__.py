"""Run the Demo runner: `uv run python -m demo_runner [--host H] [--port P]`.

Needs AGENTPAY_DEMO_AGENT_KEY and AGENTPAY_DEMO_TENANT_KEY (test keys only) and
AGENTPAY_DEMO_RUNNER_API_BASE_URL. Exits 1 before serving anything if they don't load,
saying which setting is wrong but never echoing a value.
"""

import argparse
import logging
import sys
from collections.abc import Sequence

import uvicorn
from pydantic import ValidationError

from core.settings import DemoRunnerSettings
from demo_runner.app import create_app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m demo_runner", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args(argv)
    try:
        settings = DemoRunnerSettings()  # keys from the environment
    except ValidationError as invalid:
        # Without the input: a refused key must not be printed.
        for error in invalid.errors(include_input=False, include_url=False):
            name = "AGENTPAY_" + "_".join(str(part) for part in error["loc"]).upper()
            print(f"Demo runner: {name}: {error['msg']}", file=sys.stderr)
        return 1
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
