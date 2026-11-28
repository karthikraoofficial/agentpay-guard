"""The webhook inbox (#286): the Demo runner's receiver for the Demo Tenant's Tenant
webhooks. It keeps only deliveries whose signature checks out, once per event, and hands a
run only the events about its own ids."""

from datetime import UTC, datetime, timedelta

import pytest

from core.domain.webhooks import EVENT_ID_HEADER, SIGNATURE_HEADER, body, signature
from demo.webhook_inbox import BadSignature, WebhookInbox

SECRET = "whsec_test_inbox"  # noqa: S105 - a test-only signing secret
NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)


def _delivery(
    event_id: str = "evt_1",
    event_type: str = "payment.captured",
    data: dict[str, object] | None = None,
    secret: str = SECRET,
    sent_at: datetime = NOW,
) -> tuple[bytes, dict[str, str]]:
    raw = body(event_id, event_type, NOW, data or {"payment_id": "pay_1", "mandate_id": "man_1"})
    headers = {
        SIGNATURE_HEADER: signature(secret, int(sent_at.timestamp()), raw),
        EVENT_ID_HEADER: event_id,
    }
    return raw, headers


def _inbox() -> WebhookInbox:
    return WebhookInbox(SECRET, now=lambda: NOW)


def test_a_signed_delivery_is_kept_with_its_signature_verified() -> None:
    inbox = _inbox()

    got = inbox.receive(*_delivery())

    assert (got.event_id, got.event_type, got.signature) == (
        "evt_1",
        "payment.captured",
        "verified",
    )
    assert got.data == {"payment_id": "pay_1", "mandate_id": "man_1"}
    assert got.signed_at == NOW


def test_headers_are_read_whatever_their_case() -> None:
    raw, headers = _delivery()

    got = _inbox().receive(raw, {k.lower(): v for k, v in headers.items()})

    assert got.event_id == "evt_1"


@pytest.mark.parametrize(
    "spoil",
    [
        lambda raw, h: (
            raw,
            {**h, SIGNATURE_HEADER: signature("whsec_other", int(NOW.timestamp()), raw)},
        ),
        lambda raw, h: (raw.replace(b"pay_1", b"pay_2"), h),
        lambda raw, h: (raw, {k: v for k, v in h.items() if k != SIGNATURE_HEADER}),
        lambda raw, h: (raw, {**h, SIGNATURE_HEADER: "nonsense"}),
        lambda raw, h: (
            b"not json",
            {**h, SIGNATURE_HEADER: signature(SECRET, int(NOW.timestamp()), b"not json")},
        ),
    ],
    ids=["wrong secret", "altered body", "no signature", "malformed", "not an event"],
)
def test_a_delivery_that_doesnt_verify_is_refused_and_not_kept(spoil) -> None:  # type: ignore[no-untyped-def]
    inbox = _inbox()
    raw, headers = spoil(*_delivery())

    with pytest.raises(BadSignature):
        inbox.receive(raw, headers)

    assert inbox.for_ids({"pay_1", "pay_2"}) == []


def test_a_stale_signature_is_refused() -> None:
    with pytest.raises(BadSignature):
        _inbox().receive(*_delivery(sent_at=NOW - timedelta(minutes=6)))


def test_a_redelivered_event_is_kept_once() -> None:
    inbox = _inbox()
    inbox.receive(*_delivery())

    inbox.receive(*_delivery())

    assert [e.event_id for e in inbox.for_ids({"pay_1"})] == ["evt_1"]


def test_a_run_sees_only_events_about_its_own_ids_in_arrival_order() -> None:
    inbox = _inbox()
    inbox.receive(*_delivery("evt_1", data={"payment_id": "pay_1"}))
    inbox.receive(*_delivery("evt_2", data={"payment_id": "pay_other"}))
    inbox.receive(
        *_delivery(
            "evt_3", "dispute.in_review", data={"dispute_id": "dsp_1", "payment_id": "pay_1"}
        )
    )

    mine = inbox.for_ids({"pay_1", "man_1"})

    assert [e.event_id for e in mine] == ["evt_1", "evt_3"]


def test_it_keeps_only_the_newest_events() -> None:
    inbox = WebhookInbox(SECRET, now=lambda: NOW, keep=2)
    for n in range(3):
        inbox.receive(*_delivery(f"evt_{n}", data={"payment_id": "pay_1"}))

    assert [e.event_id for e in inbox.for_ids({"pay_1"})] == ["evt_1", "evt_2"]
