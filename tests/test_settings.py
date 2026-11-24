import pytest
from pydantic import ValidationError

from core.settings import Settings
from tests.conftest import VALID, make_settings


@pytest.mark.parametrize(
    "missing", ["env", "database_url", "temporal_address", "otel_exporter_endpoint"]
)
def test_missing_required_setting_fails_fast(missing: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(f"AGENTPAY_{missing.upper()}", raising=False)
    values = {k: v for k, v in VALID.items() if k != missing}
    with pytest.raises(ValidationError, match=missing):
        Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("env", "prod"),
        ("database_url", "not a url"),
        ("temporal_address", "localhost"),
        ("otel_exporter_endpoint", "nowhere"),
    ],
)
def test_malformed_setting_fails_fast(field: str, bad: str) -> None:
    with pytest.raises(ValidationError, match=field):
        make_settings(**{field: bad})


def test_secret_never_appears_in_repr_or_str(settings: Settings) -> None:
    assert "sk-test-do-not-print" not in repr(settings)
    assert "sk-test-do-not-print" not in str(settings)
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-test-do-not-print"


def test_outside_tests_the_intent_check_needs_an_api_key_unless_it_is_off() -> None:
    with pytest.raises(ValidationError, match="AGENTPAY_ANTHROPIC_API_KEY is required"):
        make_settings(env="local", anthropic_api_key=None)

    assert make_settings(env="local", intent_check_enabled=False).anthropic_api_key is None
    assert make_settings(env="test").intent_check_timeout_ms == 2500


def test_the_rail_is_the_mock_unless_chosen() -> None:
    assert make_settings().rail == "mock"


RAZORPAY = {
    "rail": "razorpay",
    "razorpay_key_id": "rzp_test_AbCdEf123456",
    "razorpay_key_secret": "rzp-secret-do-not-print",
    "razorpay_webhook_secret": "whsec-do-not-print",
}


def test_razorpay_runs_with_a_test_key_and_both_secrets() -> None:
    settings = make_settings(**RAZORPAY)

    assert settings.rail == "razorpay"
    assert "rzp-secret-do-not-print" not in repr(settings)
    assert "whsec-do-not-print" not in repr(settings)


@pytest.mark.parametrize(
    "missing", ["razorpay_key_id", "razorpay_key_secret", "razorpay_webhook_secret"]
)
def test_razorpay_refuses_to_start_without_each_value(missing: str) -> None:
    values = {k: v for k, v in RAZORPAY.items() if k != missing}

    with pytest.raises(ValidationError, match=f"AGENTPAY_{missing.upper()}"):
        make_settings(**values)


@pytest.mark.parametrize("rail", ["razorpay", "mock"])
@pytest.mark.parametrize("key_id", ["rzp_live_AbCdEf123456", "AbCdEf123456"])
def test_a_key_that_is_not_a_test_key_is_refused_on_any_rail(rail: str, key_id: str) -> None:
    with pytest.raises(ValidationError, match="test key"):
        make_settings(**{**RAZORPAY, "rail": rail, "razorpay_key_id": key_id})


def test_an_unknown_rail_is_refused() -> None:
    with pytest.raises(ValidationError, match="rail"):
        make_settings(rail="stripe")


def test_demo_controls_are_for_the_mock_rail_only() -> None:
    """Demo controls end timers early, so they must never reach real money (ADR-0032)."""
    assert make_settings(demo_controls_tenant_id="ten_demo").demo_controls_tenant_id == "ten_demo"
    with pytest.raises(ValidationError, match="AGENTPAY_DEMO_CONTROLS_TENANT_ID"):
        make_settings(demo_controls_tenant_id="ten_demo", **RAZORPAY)


def test_the_mock_notice_period_is_for_the_mock_rail_with_mock_settle() -> None:
    """The deployed mock rail's Pre-debit notice period: only the worker's mock settle
    debits it, so it needs mock settle, and it is refused with a real rail."""
    on = make_settings(mock_notice_seconds=120, mock_settle_after_seconds=60)
    assert on.mock_notice_seconds == 120
    with pytest.raises(ValidationError, match="AGENTPAY_MOCK_NOTICE_SECONDS"):
        make_settings(mock_notice_seconds=120)
    with pytest.raises(ValidationError, match="AGENTPAY_MOCK_NOTICE_SECONDS"):
        make_settings(mock_notice_seconds=120, **RAZORPAY)


def test_a_deployment_reaches_only_its_rail_unless_it_lists_more() -> None:
    assert make_settings().reachable_rails == frozenset({"mock"})
    assert make_settings(**RAZORPAY).reachable_rails == frozenset({"razorpay"})


def test_one_deployment_can_reach_the_mock_rail_and_razorpay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hosted demo runs simulated and real journeys side by side (#300)."""
    monkeypatch.setenv("AGENTPAY_RAILS", "mock, razorpay")
    keys = {k: v for k, v in RAZORPAY.items() if k != "rail"}

    settings = make_settings(**keys)

    assert settings.rail == "mock"
    assert settings.reachable_rails == frozenset({"mock", "razorpay"})


def test_reaching_razorpay_needs_its_keys_whichever_rail_is_first() -> None:
    with pytest.raises(ValidationError, match="AGENTPAY_RAZORPAY_KEY_ID"):
        make_settings(rails=("mock", "razorpay"))


def test_the_listed_rails_include_the_main_one() -> None:
    with pytest.raises(ValidationError, match="AGENTPAY_RAILS"):
        make_settings(**RAZORPAY, rails=("mock",))


def test_mock_only_settings_need_the_mock_rail_reachable_not_first() -> None:
    """Demo controls, mock settle and the mock notice period act on mock-rail Payments only
    (ADR-0033), so a deployment that also reaches Razorpay may still use them."""
    both = {**RAZORPAY, "rails": ("razorpay", "mock")}

    settings = make_settings(
        **both,
        demo_controls_tenant_id="ten_demo",
        mock_settle_after_seconds=60,
        mock_notice_seconds=120,
    )

    assert settings.demo_controls_tenant_id == "ten_demo"
    with pytest.raises(ValidationError, match="AGENTPAY_MOCK_SETTLE_AFTER_SECONDS"):
        make_settings(**RAZORPAY, mock_settle_after_seconds=60)
