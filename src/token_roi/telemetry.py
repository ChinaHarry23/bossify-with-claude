"""OpenTelemetry export.

Optional. If `opentelemetry` is not installed, the module no-ops with a
helpful log message. This keeps the skill fully local-first: a user can run
everything without any OTEL collector, and just opt in when they have one.

Two export paths:
    1. Span export per "turn" — a span per user prompt with attributes for
       cost_tokens, roi_class, outcome_score, reuse_score. Useful for fleet
       dashboards (grafana/tempo).
    2. Metric export for rolling counters (tokens_in/out per session, memory
       writes, retrievals). Exported via the OTLP metric exporter.

Environment variables:
    OTEL_EXPORTER_OTLP_ENDPOINT   — defaults to http://localhost:4317
    OTEL_EXPORTER_OTLP_PROTOCOL   — grpc (default) or http/protobuf
    TOKEN_ROI_SERVICE_NAME        — defaults to "bossify-with-claude"
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)


def _try_import_otel():
    try:
        from opentelemetry import trace, metrics
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    except ImportError:
        return None
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    except ImportError:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        except ImportError:
            return None
    return {
        "trace": trace, "metrics": metrics,
        "Resource": Resource, "TracerProvider": TracerProvider,
        "BatchSpanProcessor": BatchSpanProcessor,
        "OTLPSpanExporter": OTLPSpanExporter,
        "MeterProvider": MeterProvider,
        "PeriodicExportingMetricReader": PeriodicExportingMetricReader,
        "OTLPMetricExporter": OTLPMetricExporter,
    }


class Telemetry:
    """OTEL façade.

    Call `Telemetry.init()` once at CLI startup if you want spans/metrics;
    skip it and every method below becomes a cheap no-op.
    """

    _singleton: "Telemetry | None" = None

    def __init__(self, modules: dict[str, Any] | None):
        self.modules = modules
        self.enabled = modules is not None
        self._tracer = None
        self._meter = None
        self._counters: dict[str, Any] = {}
        if self.enabled:
            self._bootstrap()

    @classmethod
    def init(cls, *, force: bool = False) -> "Telemetry":
        if cls._singleton is not None and not force:
            return cls._singleton
        mods = _try_import_otel()
        if mods is None:
            log.info("OTEL not installed; telemetry disabled (skill still works).")
        cls._singleton = cls(mods)
        return cls._singleton

    @classmethod
    def get(cls) -> "Telemetry":
        return cls.init()

    # ---- bootstrap ----

    def _bootstrap(self) -> None:
        assert self.modules is not None
        M = self.modules
        service = os.environ.get("TOKEN_ROI_SERVICE_NAME", "bossify-with-claude")
        resource = M["Resource"].create({"service.name": service})
        tracer_provider = M["TracerProvider"](resource=resource)
        tracer_provider.add_span_processor(
            M["BatchSpanProcessor"](M["OTLPSpanExporter"]())
        )
        M["trace"].set_tracer_provider(tracer_provider)
        self._tracer = M["trace"].get_tracer("token_roi")

        metric_reader = M["PeriodicExportingMetricReader"](M["OTLPMetricExporter"]())
        meter_provider = M["MeterProvider"](resource=resource, metric_readers=[metric_reader])
        M["metrics"].set_meter_provider(meter_provider)
        self._meter = M["metrics"].get_meter("token_roi")

        self._counters["tokens_in"] = self._meter.create_counter(
            "token_roi.tokens_in", description="input tokens by model/session")
        self._counters["tokens_out"] = self._meter.create_counter(
            "token_roi.tokens_out", description="output tokens by model/session")
        self._counters["memory_writes"] = self._meter.create_counter(
            "token_roi.memory_writes", description="memory write events")
        self._counters["retrievals"] = self._meter.create_counter(
            "token_roi.retrievals", description="retrieval queries")
        self._counters["roi_classifications"] = self._meter.create_counter(
            "token_roi.roi_classifications", description="ROI class assignments")

    # ---- spans ----

    @contextmanager
    def prompt_span(self, session_id: str, prompt_event_id: str):
        if not self.enabled or self._tracer is None:
            yield None
            return
        with self._tracer.start_as_current_span(
            "token_roi.prompt",
            attributes={
                "token_roi.session_id": session_id,
                "token_roi.prompt_event_id": prompt_event_id,
            },
        ) as span:
            yield span

    def annotate_span(self, span, **attrs) -> None:
        if span is None:
            return
        for k, v in attrs.items():
            try:
                span.set_attribute(f"token_roi.{k}", v)
            except Exception:
                pass

    # ---- counters ----

    def count(self, name: str, amount: int = 1, **attrs) -> None:
        if not self.enabled:
            return
        c = self._counters.get(name)
        if c is None:
            return
        try:
            c.add(amount, attrs)
        except Exception:
            pass

    def record_tokens(self, *, session_id: str, model: str,
                      tokens_in: int, tokens_out: int) -> None:
        self.count("tokens_in", tokens_in, session_id=session_id, model=model)
        self.count("tokens_out", tokens_out, session_id=session_id, model=model)

    def record_memory_write(self, session_id: str) -> None:
        self.count("memory_writes", 1, session_id=session_id)

    def record_retrieval(self, session_id: str, backend: str) -> None:
        self.count("retrievals", 1, session_id=session_id, backend=backend)

    def record_roi(self, scope_kind: str, roi_class: str) -> None:
        self.count("roi_classifications", 1,
                   scope_kind=scope_kind, roi_class=roi_class)
