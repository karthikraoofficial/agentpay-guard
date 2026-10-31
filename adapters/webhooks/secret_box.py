"""SecretBox adapters: Fernet (AES-128-CBC with HMAC-SHA256) keyed from settings, and a
stand-in for when no key is set (ADR-0015)."""

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from core.ports import SecretBox
from core.settings import Settings


class FernetBox:
    def __init__(self, key: SecretStr) -> None:
        try:
            self._fernet = Fernet(key.get_secret_value().encode())
        except ValueError:
            raise ValueError(
                "AGENTPAY_WEBHOOK_ENCRYPTION_KEY must be a Fernet key "
                "(32 url-safe base64-encoded bytes)"
            ) from None

    def encrypt(self, secret: str) -> str:
        return self._fernet.encrypt(secret.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            raise ValueError("the secret was encrypted with another key") from None


class UnconfiguredBox:
    """No key is set: nothing can be encrypted or decrypted, so no endpoint can be
    registered and no Tenant webhook signed."""

    def encrypt(self, secret: str) -> str:
        raise RuntimeError("AGENTPAY_WEBHOOK_ENCRYPTION_KEY is not set")

    def decrypt(self, ciphertext: str) -> str:
        raise RuntimeError("AGENTPAY_WEBHOOK_ENCRYPTION_KEY is not set")


def secret_box(settings: Settings) -> SecretBox:
    key = settings.webhook_encryption_key
    return UnconfiguredBox() if key is None else FernetBox(key)


_conforms: type[SecretBox] = FernetBox  # mypy checks the adapters match the port
_conforms_unconfigured: type[SecretBox] = UnconfiguredBox
