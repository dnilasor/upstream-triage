"""OpenTelemetry setup shared by the agent and the MCP server.

Tracing turns on when OTEL_EXPORTER_OTLP_ENDPOINT is set (the devcontainer points
it at Jaeger). Without it, every span below is a cheap no-op, so the code paths
are identical with and without a backend.
"""

from __future__ import annotations

import os

from opentelemetry import trace

_enabled = False


def setup(service_name: str) -> bool:
    """Install an OTLP/HTTP exporter for this process. Returns True if tracing is on."""
    global _enabled
    if _enabled:
        return True
    if not (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")):
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:  # SDK not installed: stay a no-op rather than fail the demo
        return False

    from upstream import __version__

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "upstream-triage",
            "service.version": __version__,
        }
    )
    provider = TracerProvider(resource=resource)
    # Short delay so spans from short-lived processes (the stdio server) get flushed.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000))
    trace.set_tracer_provider(provider)
    _enabled = True
    return True


def shutdown() -> None:
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()


def tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)
