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
    kind: str = "histogram"  # or "counter", "gauge"


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

    def count(self, name: str, attributes: Attributes, value: int = 1) -> None:
        self.metrics.append(RecordedMetric(name, value, dict(attributes), "counter"))

    def set_gauge(self, name: str, value: float, attributes: Attributes) -> None:
        self.metrics.append(RecordedMetric(name, value, dict(attributes), "gauge"))

    def recorded(self, name: str, **attributes: AttributeValue) -> list[RecordedMetric]:
        """Every recording of `name` whose attributes include `attributes`."""
        return [
            m
            for m in self.metrics
            if m.name == name and all(m.attributes.get(k) == v for k, v in attributes.items())
        ]

    def total(self, name: str, **attributes: AttributeValue) -> float:
        """What a counter adds up to, over the attribute sets that include `attributes`."""
        return sum(m.value for m in self.recorded(name, **attributes))

    def last(self, name: str, **attributes: AttributeValue) -> float | None:
        """A gauge's latest value, or None if it was never set."""
        found = self.recorded(name, **attributes)
        return found[-1].value if found else None
