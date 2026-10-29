"""The OpenTelemetry Telemetry adapter: the only module that imports OpenTelemetry.

Spans and metrics are exported over OTLP/HTTP to the collector in infra/ (settings
`otel_exporter_endpoint`). With `telemetry_exporter = "none"` the adapter still builds
real spans but exports nothing, so tests and CI run it without a collector. Callers must
pass identifiers, codes and numbers only, never free text (Payer instructions, merchant
names, line items, Agent reasoning).
"""

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Histogram
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Span

from core.ports import Attributes
from core.settings import Settings


class OtelTelemetry:
    def __init__(
        self,
        service_name: str,
        service_version: str,
        span_processor: SpanProcessor | None = None,
        metric_reader: MetricReader | None = None,
    ) -> None:
        resource = Resource.create(
            {"service.name": service_name, "service.version": service_version}
        )
        # Own providers rather than the global ones, so two apps in one process (as in
        # tests) never share or overwrite each other's pipeline.
        self._tracer_provider = TracerProvider(resource=resource)
        if span_processor is not None:
            self._tracer_provider.add_span_processor(span_processor)
        readers = [metric_reader] if metric_reader is not None else []
        self._meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        self._tracer = self._tracer_provider.get_tracer("agentpay_guard")
        self._meter = self._meter_provider.get_meter("agentpay_guard")
        self._histograms: dict[str, Histogram] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> "OtelTelemetry":
        if settings.telemetry_exporter == "none":
            return cls(settings.app_name, settings.app_version)
        endpoint = str(settings.otel_exporter_endpoint).rstrip("/")
        span_exporter: SpanExporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")
        return cls(
            settings.app_name,
            settings.app_version,
            span_processor=BatchSpanProcessor(span_exporter),
            metric_reader=PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics")
            ),
        )

    @contextmanager
    def start_span(self, name: str, attributes: Attributes) -> Iterator[Span]:
        with self._tracer.start_as_current_span(name, attributes=dict(attributes)) as span:
            yield span

    def record_metric(self, name: str, value: float, attributes: Attributes) -> None:
        histogram = self._histograms.get(name)
        if histogram is None:
            histogram = self._histograms[name] = self._meter.create_histogram(name)
        histogram.record(value, attributes=dict(attributes))

    def shutdown(self) -> None:
        """Flush and stop exporting. Called when the app shuts down."""
        self._tracer_provider.shutdown()
        self._meter_provider.shutdown()
