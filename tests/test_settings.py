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
