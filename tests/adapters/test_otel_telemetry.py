"""The OpenTelemetry adapter behind the Telemetry port, exporting to in-memory readers."""

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from adapters.otel.telemetry import OtelTelemetry
from tests.conftest import make_settings


def test_a_span_is_exported_with_its_name_and_attributes() -> None:
    exporter = InMemorySpanExporter()
    telemetry = OtelTelemetry("svc", "1.2.3", span_processor=SimpleSpanProcessor(exporter))

    with telemetry.start_span("decision", {"tenant_id": "ten_1"}) as span:
        span.set_attribute("outcome", "allow")

    (exported,) = exporter.get_finished_spans()
    assert exported.name == "decision"
    assert dict(exported.attributes or {}) == {"tenant_id": "ten_1", "outcome": "allow"}
    assert exported.resource.attributes["service.name"] == "svc"


def test_a_metric_is_recorded_as_a_histogram() -> None:
    reader = InMemoryMetricReader()
    telemetry = OtelTelemetry("svc", "1.2.3", metric_reader=reader)

    telemetry.record_metric("decision.latency_ms", 12.5, {"outcome": "allow"})
    telemetry.record_metric("decision.latency_ms", 7.5, {"outcome": "allow"})

    data = reader.get_metrics_data()
    assert data is not None
    (metric,) = [m for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics]
    (point,) = metric.data.data_points
    assert (metric.name, point.count, point.sum) == ("decision.latency_ms", 2, 20.0)


def test_with_no_exporter_spans_still_work_and_nothing_is_sent() -> None:
    telemetry = OtelTelemetry.from_settings(make_settings(telemetry_exporter="none"))

    with telemetry.start_span("decision", {"tenant_id": "ten_1"}) as span:
        span.set_attribute("outcome", "block")
    telemetry.shutdown()
