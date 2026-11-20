"""The in-memory Telemetry keeps what each metric call recorded, and of which kind."""

from adapters.memory.telemetry import InMemoryTelemetry


def test_each_kind_of_metric_is_kept_with_its_kind() -> None:
    telemetry = InMemoryTelemetry()

    telemetry.record_metric("h", 1.5, {"a": "x"})
    telemetry.count("c", {"a": "y"})
    telemetry.count("c", {"a": "y"}, 2)
    telemetry.set_gauge("g", 3, {})

    assert [(m.kind, m.name, m.value) for m in telemetry.metrics] == [
        ("histogram", "h", 1.5),
        ("counter", "c", 1),
        ("counter", "c", 2),
        ("gauge", "g", 3),
    ]
    assert telemetry.total("c", a="y") == 3
    assert telemetry.last("g") == 3
    assert telemetry.last("missing") is None
