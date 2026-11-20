"""The metric alert rules (#228): kept in `infra/grafana/alert-rules.yaml`, each naming a
runbook that names it back, over metrics the app emits, and pushed to Grafana through its
provisioning API (here a stand-in transport, so no Grafana account)."""

import json
import re
from pathlib import Path

import httpx
import pytest

import core.ops_metrics as ops_metrics
from adapters.grafana.alerts import (
    CONTACT_POINT,
    RULES,
    AlertRule,
    contact_point,
    load_rules,
    push,
    rule_group,
    send_test_alert,
)

DATASOURCE = "grafanacloud-prom"


def emitted_series() -> set[str]:
    """Every Prometheus series name the app's metrics become in Grafana Cloud."""
    names = {
        value
        for key, value in vars(ops_metrics).items()
        if key.isupper() and isinstance(value, str) and value.startswith("agentpay.")
    }
    series = set()
    for name in names:
        base = name.replace(".", "_")
        series |= {base, f"{base}_total", f"{base}_bucket", f"{base}_sum", f"{base}_count"}
    return series


@pytest.fixture(scope="module")
def rules() -> list[AlertRule]:
    return load_rules(RULES)


def test_every_rule_names_a_runbook_that_names_it_back(rules: list[AlertRule]) -> None:
    for rule in rules:
        runbook = Path(rule.runbook)
        assert runbook.is_file(), rule.uid
        assert rule.title in runbook.read_text(encoding="utf-8"), (rule.uid, rule.runbook)


def test_every_rule_reads_a_metric_the_app_emits(rules: list[AlertRule]) -> None:
    emitted = emitted_series()
    for rule in rules:
        used = set(re.findall(r"\bagentpay_[a-z_]+", rule.expr))
        assert used, rule.uid
        assert used <= emitted, (rule.uid, used - emitted)


def test_each_runbook_symptom_has_an_alert(rules: list[AlertRule]) -> None:
    runbooks = {r.runbook for r in rules}
    assert {
        "docs/runbooks/rail-outage.md",
        "docs/runbooks/model-outage.md",
        "docs/runbooks/stuck-consent-workflows.md",
    } <= runbooks


def test_every_rule_has_a_written_threshold_and_a_unique_id(rules: list[AlertRule]) -> None:
    assert len({r.uid for r in rules}) == len(rules)
    for rule in rules:
        assert len(rule.uid) <= 40  # Grafana's limit
        assert rule.threshold
        assert rule.no_data in {"OK", "Alerting"}


def test_a_rule_becomes_a_grafana_rule_with_the_threshold_applied_by_grafana(
    rules: list[AlertRule],
) -> None:
    group = rule_group(rules, folder_uid="agentpay-guard", datasource_uid=DATASOURCE)

    assert (group["title"], group["folderUid"], group["interval"]) == (
        "agentpay-guard",
        "agentpay-guard",
        60,
    )
    [breaker] = [r for r in group["rules"] if r["uid"] == "agentpay-rail-breaker-open"]
    query, threshold = breaker["data"]
    assert query["datasourceUid"] == DATASOURCE
    assert query["model"]["expr"] == "max(agentpay_rail_breaker_open)"
    assert query["model"]["instant"] is True
    assert threshold["datasourceUid"] == "__expr__"
    assert threshold["model"]["type"] == "threshold"
    assert threshold["model"]["conditions"][0]["evaluator"] == {"type": "gt", "params": [0]}
    assert breaker["condition"] == threshold["refId"]
    assert breaker["for"] == "5m"
    assert breaker["noDataState"] == "OK"
    assert breaker["execErrState"] == "Error"
    assert breaker["annotations"]["runbook_url"].endswith("docs/runbooks/rail-outage.md")
    assert "5 minutes" in breaker["annotations"]["summary"]
    assert breaker["notification_settings"] == {"receiver": CONTACT_POINT}


def test_the_contact_point_emails_the_address_given() -> None:
    point = contact_point("ops@example.com")

    assert point["name"] == CONTACT_POINT
    assert point["type"] == "email"
    assert point["settings"] == {"addresses": "ops@example.com"}


class FakeGrafana:
    """Grafana's provisioning API, enough of it to see what a push asks for."""

    def __init__(self, *, folder: bool = False, contact_point_uid: str | None = None) -> None:
        self.folder = folder
        self.contact_point_uid = contact_point_uid
        self.requests: list[tuple[str, str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        assert request.headers["Authorization"] == "Bearer glsa_token"
        path, method = request.url.path, request.method
        if path == "/api/folders/agentpay-guard" and method == "GET":
            return httpx.Response(200 if self.folder else 404, json={})
        if path == "/api/v1/provisioning/contact-points" and method == "GET":
            found = [] if self.contact_point_uid is None else [{"uid": self.contact_point_uid}]
            return httpx.Response(200, json=found)
        return httpx.Response(200 if method == "PUT" else 201, json={})

    def calls(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path, _ in self.requests]


def client(grafana: FakeGrafana) -> httpx.Client:
    return httpx.Client(
        base_url="https://agentpay.grafana.net",
        headers={"Authorization": "Bearer glsa_token"},
        transport=httpx.MockTransport(grafana),
    )


def test_a_first_push_makes_the_folder_and_contact_point_then_puts_the_group(
    rules: list[AlertRule],
) -> None:
    grafana = FakeGrafana()

    push(client(grafana), rules, "ops@example.com", DATASOURCE)

    assert grafana.calls() == [
        ("GET", "/api/folders/agentpay-guard"),
        ("POST", "/api/folders"),
        ("GET", "/api/v1/provisioning/contact-points"),
        ("POST", "/api/v1/provisioning/contact-points"),
        ("PUT", "/api/v1/provisioning/folder/agentpay-guard/rule-groups/agentpay-guard"),
    ]
    _, _, group = grafana.requests[-1]
    assert isinstance(group, dict)
    assert len(group["rules"]) == len(rules)


def test_a_second_push_updates_in_place(rules: list[AlertRule]) -> None:
    grafana = FakeGrafana(folder=True, contact_point_uid="cp-1")

    push(client(grafana), rules, "ops@example.com", DATASOURCE)

    assert grafana.calls() == [
        ("GET", "/api/folders/agentpay-guard"),
        ("GET", "/api/v1/provisioning/contact-points"),
        ("PUT", "/api/v1/provisioning/contact-points/cp-1"),
        ("PUT", "/api/v1/provisioning/folder/agentpay-guard/rule-groups/agentpay-guard"),
    ]


def test_a_refused_request_stops_the_push_without_showing_the_token(
    rules: list[AlertRule],
) -> None:
    def refuses(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "invalid API key"})

    refusing = httpx.Client(
        base_url="https://agentpay.grafana.net",
        headers={"Authorization": "Bearer glsa_token"},
        transport=httpx.MockTransport(refuses),
    )

    with pytest.raises(RuntimeError) as refused:
        push(refusing, rules, "ops@example.com", DATASOURCE)

    assert "401" in str(refused.value)
    assert "glsa_token" not in str(refused.value)


def test_a_test_alert_goes_through_the_receivers_api_to_the_email_integration() -> None:
    receivers = "/apis/notifications.alerting.grafana.app/v1beta1/namespaces/stacks-42/receivers"
    integration = {"uid": "int-1", "type": "email", "settings": {"addresses": "ops@example.com"}}
    seen: list[tuple[str, str, object]] = []

    def grafana(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.url.path == "/api/frontend/settings":
            return httpx.Response(200, json={"namespace": "stacks-42"})
        if request.url.path == receivers:
            items = [
                {
                    "metadata": {"name": "other"},
                    "spec": {"title": "someone-else", "integrations": []},
                },
                {
                    "metadata": {"name": "rcv-1"},
                    "spec": {"title": CONTACT_POINT, "integrations": [integration]},
                },
            ]
            return httpx.Response(200, json={"items": items})
        return httpx.Response(200, json={"status": "success"})

    grafana_client = httpx.Client(
        base_url="https://agentpay.grafana.net",
        headers={"Authorization": "Bearer glsa_token"},
        transport=httpx.MockTransport(grafana),
    )

    send_test_alert(grafana_client)

    assert seen[-1] == ("POST", f"{receivers}/rcv-1/test", {"integration": integration})


def test_a_test_alert_without_the_contact_point_says_to_push_first() -> None:
    def grafana(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/frontend/settings":
            return httpx.Response(200, json={"namespace": "stacks-42"})
        return httpx.Response(200, json={"items": []})

    grafana_client = httpx.Client(
        base_url="https://agentpay.grafana.net", transport=httpx.MockTransport(grafana)
    )

    with pytest.raises(RuntimeError, match="push the rules first"):
        send_test_alert(grafana_client)
