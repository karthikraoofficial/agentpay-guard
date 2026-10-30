"""Telemetry at the HTTP seam, with the in-memory Telemetry fake behind the port."""

from tests.api.conftest import Api
from tests.api.test_decisions import Pay


def test_every_decision_emits_a_decision_span(api: Api) -> None:
    pay = Pay(api)

    body = pay.ask(amount={"minor": 6_000_00, "currency": "INR"}).json()

    spans = [s for s in api.telemetry.spans if s.name == "decision"]
    assert len(spans) == 1
    assert spans[0].attributes == {
        "tenant_id": pay.tenant_id(),
        "outcome": "step_up",
        "primary_reason_code": "AMOUNT_ABOVE_STEP_UP_THRESHOLD",
        "pack_id": "default",
        "pack_version": "1.3.0",
        "decision_id": body["decision_id"],
    }


def test_an_allow_has_no_primary_reason_code(api: Api) -> None:
    Pay(api).ask()

    (span,) = [s for s in api.telemetry.spans if s.name == "decision"]
    assert span.attributes["outcome"] == "allow"
    assert "primary_reason_code" not in span.attributes


SENTINEL = "zz-sentinel-text"


def test_no_free_text_from_the_request_reaches_any_span(api: Api) -> None:
    pay = Pay(api)

    pay.ask(
        merchant={"id": "m_1", "name": f"Shop {SENTINEL}", "mcc": "5411"},
        agent_reasoning=f"Because {SENTINEL}",
        line_items=[
            {
                "description": f"Item {SENTINEL}",
                "quantity": 1,
                "unit_amount": {"minor": 1_250_00, "currency": "INR"},
            }
        ],
    )

    assert api.telemetry.spans
    for span in api.telemetry.spans:
        for key, value in span.attributes.items():
            assert SENTINEL not in str(value), f"{span.name}.{key} leaks request text"
