"""Agent-loop tests with a scripted fake Claude and the in-process MCP server (no network, no API key)."""

import asyncio
from types import SimpleNamespace as NS

import pytest
from mcp import Client
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from upstream import agent as agent_mod
from upstream import server
from tests.test_server import fake_get_json

EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)


def usage(i=1000, o=200):
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=0)


def tool_use(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


class FakeClaude:
    """Turn 1: two parallel tool calls (one invalid). Turn 2: final answer."""

    def __init__(self):
        self.requests = []
        self.messages = NS(create=self.create)

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if len(self.requests) == 1:
            content = [
                NS(type="text", text="Checking Spain's signals and events."),
                tool_use("t1", "internet-signals__ioda_signal_summary", entity_type="country", entity_code="ES",
                         start="2025-04-28T08:00Z", end="2025-04-28T11:00Z"),
                tool_use("t2", "internet-signals__ioda_outage_events", entity_type="asn", entity_code="not-an-asn",
                         start="2025-04-28T08:00Z", end="2025-04-28T11:00Z"),
            ]
            return NS(id="m1", model="fake", stop_reason="tool_use", content=content, usage=usage())
        return NS(id="m2", model="fake", stop_reason="end_turn",
                  content=[NS(type="text", text="**Verdict**: Spain-wide outage.")], usage=usage(1500, 300))


class InProcAgent(agent_mod.TriageAgent):
    async def __aenter__(self):
        client = await self._stack.enter_async_context(Client(server.mcp))
        self._clients["internet-signals"] = client
        for tool in (await client.list_tools()).tools:
            name = f"internet-signals__{tool.name}"
            self._tools[name] = ("internet-signals", tool.name)
            self.tool_specs.append({"name": name, "description": tool.description, "input_schema": tool.input_schema})
        return self


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(server, "get_json", fake_get_json)
    EXPORTER.clear()


def test_loop_runs_tools_and_traces_everything():
    fake = FakeClaude()

    async def go():
        async with InProcAgent([], model="fake-model") as a:
            a._llm = fake
            return await a.investigate("Is Spain down?", now="2025-04-28T13:00Z")

    answer, stats = asyncio.run(go())
    assert answer == "**Verdict**: Spain-wide outage."
    assert (stats.llm_calls, stats.tool_calls, stats.tool_errors) == (2, 2, 1)
    assert stats.input_tokens == 2500

    # Replay mode is in the system prompt; tool results went back with is_error set correctly.
    first = fake.requests[0]
    assert "REPLAY MODE" in first["system"][0]["text"] and "2025-04-28T13:00Z" in first["system"][0]["text"]
    results = fake.requests[1]["messages"][2]["content"]  # [question, tool calls, tool results, ...]
    assert [r["is_error"] for r in results] == [False, True]

    spans = {s.name: s for s in EXPORTER.get_finished_spans()}
    root = spans["invoke_agent upstream-triage"]
    assert root.attributes["upstream.tool_errors"] == 1
    assert spans["chat fake-model"].attributes["gen_ai.usage.input_tokens"] in (1000, 1500)
    bad = spans["execute_tool ioda_outage_events"]
    assert bad.status.status_code.name == "ERROR" and bad.attributes["error.type"] == "tool_error"
    # Server-side span for the same call exists in the same trace (context propagated through MCP _meta).
    server_span = spans["tools/call ioda_outage_events"]
    assert server_span.context.trace_id == root.context.trace_id


def test_last_turn_forces_an_answer():
    fake = FakeClaude()

    async def go():
        async with InProcAgent([], model="fake-model", max_turns=1) as a:
            a._llm = fake
            return await a.investigate("Is Spain down?")

    asyncio.run(go())
    assert fake.requests[0]["tool_choice"] == {"type": "none"}


def test_server_config_env_expansion(tmp_path, monkeypatch):
    cfg = tmp_path / "servers.json"
    cfg.write_text(
        '{"servers": {"local": {"command": "python", "args": ["-m", "upstream.server"]},'
        ' "radar": {"url": "https://radar.example/mcp", "headers": {"Authorization": "Bearer ${TOKEN_X}"},'
        ' "requires_env": ["TOKEN_X"]}}}'
    )
    monkeypatch.delenv("TOKEN_X", raising=False)
    assert [c.name for c in agent_mod.load_server_configs(str(cfg), log=lambda _: None)] == ["local"]
    monkeypatch.setenv("TOKEN_X", "abc")
    radar = agent_mod.load_server_configs(str(cfg), log=lambda _: None)[1]
    assert radar.headers == {"Authorization": "Bearer abc"}


def test_stdio_transport_span_stays_green_on_tool_error():
    """The finding this demo is built around, checked over a real stdio connection.

    A tool error comes back as a successful JSON-RPC response with isError=true, so the MCP SDK's
    transport span ("MCP send tools/call ...") is not marked as an error. Only our execute_tool span is.
    """
    import sys

    cfg = agent_mod.ServerConfig(name="internet-signals", command=sys.executable, args=["-m", "upstream.server"])

    async def go():
        async with agent_mod.TriageAgent([cfg]) as a:
            # Invalid input fails inside the server before any network call.
            return await a.call_tool("internet-signals__ioda_outage_events",
                                     {"entity_type": "asn", "entity_code": "Optus", "start": "now-1d", "end": "now"})

    text, is_error = asyncio.run(go())
    assert is_error and "not an ASN" in text
    spans = {s.name: s for s in EXPORTER.get_finished_spans()}
    assert spans["execute_tool ioda_outage_events"].status.status_code.name == "ERROR"
    transport = spans["MCP send tools/call ioda_outage_events"]
    assert transport.status.status_code.name != "ERROR"
    assert transport.parent.span_id == spans["execute_tool ioda_outage_events"].context.span_id
