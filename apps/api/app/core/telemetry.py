"""OpenTelemetry request instrumentation with optional OTLP export."""

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.core.config import get_settings

_telemetry_shutdown = False


def configure_telemetry() -> None:
    """Install SDK providers; OTLP exporters are enabled only when configured."""
    settings = get_settings()
    resource = Resource.create({SERVICE_NAME: settings.otel_service_name})
    traces = TracerProvider(resource=resource)
    metric_readers = []
    if settings.otel_exporter_otlp_endpoint:
        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")
        traces.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
        )
        metric_readers.append(
            PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"))
        )
    trace.set_tracer_provider(traces)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=metric_readers))


def shutdown_telemetry() -> None:
    """Flush and close telemetry exporters during orderly process shutdown."""
    global _telemetry_shutdown
    if _telemetry_shutdown:
        return
    _telemetry_shutdown = True
    tracer_provider = trace.get_tracer_provider()
    meter_provider = metrics.get_meter_provider()
    if isinstance(tracer_provider, TracerProvider):
        tracer_provider.force_flush()
        tracer_provider.shutdown()
    if isinstance(meter_provider, MeterProvider):
        meter_provider.force_flush()
        meter_provider.shutdown()


def flush_telemetry() -> None:
    """Flush telemetry at app lifespan boundaries without closing shared SDK providers."""
    tracer_provider = trace.get_tracer_provider()
    meter_provider = metrics.get_meter_provider()
    if isinstance(tracer_provider, TracerProvider):
        tracer_provider.force_flush()
    if isinstance(meter_provider, MeterProvider):
        meter_provider.force_flush()


configure_telemetry()
tracer = trace.get_tracer("forge.api")
meter = metrics.get_meter("forge.api")
request_count = meter.create_counter("http.server.requests", unit="{request}")
request_duration = meter.create_histogram("http.server.duration", unit="ms")
worker_job_count = meter.create_counter("forge.worker.jobs", unit="{job}")
worker_job_duration = meter.create_histogram("forge.worker.job.duration", unit="ms")
