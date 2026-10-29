"""An in-memory Telemetry for tests: keeps every span and metric so tests can read them."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from core.ports import Attributes, AttributeValue


@dataclass
class RecordedSpan:
    name: str
    attributes: dict[str, AttributeValue] = field(default_factory=dict)

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        self.attributes[key] = value


@dataclass(frozen=True)
class RecordedMetric:
    name: str
    value: float
    attributes: dict[str, AttributeValue]


class InMemoryTelemetry:
    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []
        self.metrics: list[RecordedMetric] = []

    @contextmanager
    def start_span(self, name: str, attributes: Attributes) -> Iterator[RecordedSpan]:
        span = RecordedSpan(name, dict(attributes))
        self.spans.append(span)
        yield span

    def record_metric(self, name: str, value: float, attributes: Attributes) -> None:
        self.metrics.append(RecordedMetric(name, value, dict(attributes)))
