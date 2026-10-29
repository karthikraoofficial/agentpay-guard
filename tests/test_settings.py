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
