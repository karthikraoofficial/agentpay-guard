"""The ModelGateway for the API and the worker alike."""

from adapters.claude.gateway import ClaudeGateway
from adapters.memory.model_gateway import UnconfiguredGateway
from core.ports import ModelGateway, Telemetry
from core.settings import Settings


def build_gateway(settings: Settings, telemetry: Telemetry) -> ModelGateway:
    """Claude when a key is configured. Without one every call fails, so the Intent check
    gives `unknown` and a Dispute's narrative is written by code: both fail closed."""
    if settings.anthropic_api_key is None:
        return UnconfiguredGateway()
    return ClaudeGateway(settings.anthropic_api_key, telemetry)
