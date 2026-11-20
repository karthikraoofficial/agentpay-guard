"""Push the metric alert rules in `infra/grafana/alert-rules.yaml` to Grafana Cloud (#228).

    uv run python -m adapters.grafana.alerts            # push
    uv run python -m adapters.grafana.alerts --dry-run  # print the rule group, send nothing
    uv run python -m adapters.grafana.alerts --test-alert  # email a test notification

Run on the operator's machine, not the VM. Needs AGENTPAY_GRAFANA_URL (the stack,
https://<stack>.grafana.net), AGENTPAY_GRAFANA_API_TOKEN (a service account token with the
Admin role: Editor can't write provisioned alert rules, which needs alert.provisioning:write)
and AGENTPAY_ALERT_EMAIL in `.env`. It makes the folder and the `agentpay-email`
contact point if they're missing, then replaces the whole rule group, so a second push
updates the rules in place. Rules and the contact point pushed this way are read-only in
Grafana's UI, which also hides the contact point's Test button: change the YAML and push
again, and send a test with --test-alert. The token is never printed.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from core.settings import get_settings

RULES = Path(__file__).resolve().parents[2] / "infra" / "grafana" / "alert-rules.yaml"
CONTACT_POINT = "agentpay-email"
REPO = "https://github.com/karthikraoofficial/agentpay-guard/blob/main/"
# How far back each query may look: the longest window in the rules is an hour.
LOOKBACK_SECONDS = 3600


@dataclass(frozen=True)
class AlertRule:
    uid: str
    title: str
    runbook: str
    expr: str
    above: float
    for_: str
    no_data: str
    threshold: str


@dataclass(frozen=True)
class RuleFile:
    folder: str
    group: str
    interval_seconds: int
    rules: list[AlertRule]


def _seconds(duration: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600}
    return int(duration[:-1]) * units[duration[-1]]


def load_file(path: Path = RULES) -> RuleFile:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = [
        AlertRule(
            uid=r["uid"],
            title=r["title"],
            runbook=r["runbook"],
            expr=" ".join(str(r["expr"]).split()),
            above=float(r["above"]),
            for_=r["for"],
            no_data=r["no_data"],
            threshold=r["threshold"],
        )
        for r in raw["rules"]
    ]
    return RuleFile(raw["folder"], raw["group"], _seconds(raw["interval"]), rules)


def load_rules(path: Path = RULES) -> list[AlertRule]:
    return load_file(path).rules


def _grafana_rule(
    rule: AlertRule, folder_uid: str, group: str, datasource_uid: str
) -> dict[str, Any]:
    above = int(rule.above) if rule.above.is_integer() else rule.above
    return {
        "uid": rule.uid,
        "title": rule.title,
        "folderUID": folder_uid,
        "ruleGroup": group,
        "orgID": 1,
        "condition": "B",
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": LOOKBACK_SECONDS, "to": 0},
                "datasourceUid": datasource_uid,
                "model": {"refId": "A", "expr": rule.expr, "instant": True, "range": False},
            },
            {
                "refId": "B",
                "relativeTimeRange": {"from": 0, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "B",
                    "type": "threshold",
                    "expression": "A",
                    "conditions": [{"evaluator": {"type": "gt", "params": [above]}}],
                },
            },
        ],
        "for": rule.for_,
        "noDataState": rule.no_data,
        "execErrState": "Error",
        "annotations": {
            "summary": f"Fires when {rule.threshold}.",
            "runbook_url": REPO + rule.runbook,
        },
        "labels": {"service": "agentpay-guard"},
        "notification_settings": {"receiver": CONTACT_POINT},
    }


def rule_group(
    rules: list[AlertRule],
    folder_uid: str,
    datasource_uid: str,
    group: str = "agentpay-guard",
    interval_seconds: int = 60,
) -> dict[str, Any]:
    """The Grafana AlertRuleGroup that `PUT .../rule-groups/{group}` takes."""
    return {
        "title": group,
        "folderUid": folder_uid,
        "interval": interval_seconds,
        "rules": [_grafana_rule(r, folder_uid, group, datasource_uid) for r in rules],
    }


def contact_point(email: str) -> dict[str, Any]:
    return {
        "name": CONTACT_POINT,
        "type": "email",
        "settings": {"addresses": email},
        "disableResolveMessage": False,
    }


def _check(response: httpx.Response) -> httpx.Response:
    """Raise with the status and Grafana's message only: never the request's headers."""
    if response.is_error:
        try:
            message = response.json().get("message", "")
        except ValueError:
            message = response.text[:200]
        raise RuntimeError(
            f"Grafana answered {response.status_code} to {response.request.method} "
            f"{response.request.url.path}: {message}"
        )
    return response


def push(
    client: httpx.Client,
    rules: list[AlertRule],
    email: str,
    datasource_uid: str,
    folder: str = "agentpay-guard",
    group: str = "agentpay-guard",
    interval_seconds: int = 60,
) -> None:
    if client.get(f"/api/folders/{folder}").status_code == 404:
        _check(client.post("/api/folders", json={"uid": folder, "title": "AgentPay Guard"}))
    found = _check(
        client.get("/api/v1/provisioning/contact-points", params={"name": CONTACT_POINT})
    ).json()
    if found:
        uid = found[0]["uid"]
        _check(client.put(f"/api/v1/provisioning/contact-points/{uid}", json=contact_point(email)))
    else:
        _check(client.post("/api/v1/provisioning/contact-points", json=contact_point(email)))
    body = rule_group(rules, folder, datasource_uid, group, interval_seconds)
    _check(client.put(f"/api/v1/provisioning/folder/{folder}/rule-groups/{group}", json=body))


def send_test_alert(client: httpx.Client) -> None:
    """Have Grafana send the contact point a test notification, through its receivers API
    (the older test endpoint is gone). The stack's namespace comes from Grafana itself."""
    namespace = _check(client.get("/api/frontend/settings")).json()["namespace"]
    receivers = f"/apis/notifications.alerting.grafana.app/v1beta1/namespaces/{namespace}/receivers"
    items = _check(client.get(receivers)).json().get("items", [])
    found = [r for r in items if r["spec"]["title"] == CONTACT_POINT]
    if not found or not found[0]["spec"]["integrations"]:
        raise RuntimeError(f"Grafana has no {CONTACT_POINT} contact point: push the rules first")
    receiver = found[0]
    integration = receiver["spec"]["integrations"][0]
    name = receiver["metadata"]["name"]
    _check(client.post(f"{receivers}/{name}/test", json={"integration": integration}))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the rule group only")
    parser.add_argument(
        "--test-alert", action="store_true", help="email the contact point a test notification"
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    spec = load_file()
    if args.dry_run:
        body = rule_group(
            spec.rules,
            spec.folder,
            settings.grafana_datasource_uid,
            spec.group,
            spec.interval_seconds,
        )
        print(json.dumps(body, indent=2))
        return 0
    if (
        settings.grafana_url is None
        or settings.grafana_api_token is None
        or not settings.alert_email
    ):
        print(
            "Set AGENTPAY_GRAFANA_URL, AGENTPAY_GRAFANA_API_TOKEN and AGENTPAY_ALERT_EMAIL in .env",
            file=sys.stderr,
        )
        return 2
    token = settings.grafana_api_token.get_secret_value()
    with httpx.Client(
        base_url=str(settings.grafana_url).rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    ) as client:
        try:
            if args.test_alert:
                send_test_alert(client)
                print(f"sent a test notification to {CONTACT_POINT}: check the inbox (and spam)")
                return 0
            push(
                client,
                spec.rules,
                settings.alert_email,
                settings.grafana_datasource_uid,
                spec.folder,
                spec.group,
                spec.interval_seconds,
            )
        except RuntimeError as refused:
            print(refused, file=sys.stderr)
            return 1
    pushed = f"{spec.folder}/{spec.group}"
    print(f"pushed {len(spec.rules)} alert rule(s) to {pushed}, emailing {CONTACT_POINT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
