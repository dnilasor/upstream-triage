"""The triage agent: Claude plus any number of MCP servers, traced with OpenTelemetry.

The loop is deliberately hand-written (Anthropic SDK + MCP Python SDK) rather than
hidden behind a framework, so every step is visible and gets its own span:

  invoke_agent upstream-triage            <- one per investigation
    chat claude-...                       <- one per model call (tokens, stop reason)
    execute_tool internet-signals__...    <- one per tool call (tool errors marked)
      MCP send tools/call ...             <- emitted by the MCP SDK; carries traceparent
        tools/call ...                    <- server side, in the MCP server's process
          GET api.ioda...                 <- outbound API call from the server

Span names and attributes follow the OpenTelemetry GenAI semantic conventions,
which are still marked "Development", so expect names to shift between releases.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from opentelemetry.trace import SpanKind, StatusCode

from upstream import telemetry
from upstream.timeutil import iso

DEFAULT_MODEL = os.environ.get("UPSTREAM_MODEL", "claude-sonnet-5-5")
MAX_TOOL_RESULT_CHARS = 12_000
CAPTURE_CONTENT = os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "").lower() == "true"

tracer = telemetry.tracer("upstream.agent")

SYSTEM_PROMPT = """\
You are Upstream, an incident-triage assistant for on-call engineers. Your job is to answer one
question fast and honestly: is this problem in the user's own systems, in a specific network or ISP,
in a whole country or region, or at a SaaS vendor?

How to investigate:
- Work outward in layers: the affected network (ASN/prefix) -> the country -> other networks in the
  same place -> vendors. Stop as soon as the evidence is conclusive; most cases need 4-10 tool calls.
- Call independent tools in parallel in a single turn.
- For time-series tools, start the window a few hours BEFORE the reported onset, so there is a baseline.
- Always use explicit UTC times. Quote the numbers you rely on (e.g. "BGP visibility fell to 12% of
  baseline at 10:35Z").
- Base conclusions only on what the tools returned. If a source has no data, say so; "no data" is not
  "no problem". Absence of an internet-level signal is itself useful evidence that the network is
  probably innocent ("mean time to innocence").

Finish with exactly these sections, in Markdown:
**Verdict**: one sentence.
**Confidence**: high / medium / low, and why.
**Evidence**: 3-6 bullets, each with a number, a time and its source.
**What would change my mind**: 1-2 bullets.
**Next steps for on-call**: 2-4 concrete bullets.
"""

REPLAY_ADDENDUM = """\

REPLAY MODE. This is a replay of a past incident. Treat {now} as the current time.
- Use absolute ISO-8601 UTC timestamps in every tool call; never 'now' or 'now-6h'.
- Live-only tools (bgp_routing_status, service_status) show today's state, not {now}; don't rely on them.
- Reason only from the tool evidence, the way an on-call engineer would have at {now}. Do not lean on
  what you may know about how this event was later explained publicly.
"""

JUDGE_PROMPT = """\
You are grading an incident-triage agent. Compare its answer to the reference diagnosis.
Reference diagnosis: {expect}

Agent's answer:
<answer>
{answer}
</answer>

Grade "pass" if the verdict matches the reference's core conclusion (which layer is at fault and roughly
when), "partial" if it is on the right track but misses or hedges the core point, "fail" otherwise.
Respond with only JSON: {{"grade": "pass" | "partial" | "fail", "reason": "<one sentence>"}}
"""


# --------------------------------------------------------------------------- #
# Server configuration
# --------------------------------------------------------------------------- #


@dataclass
class ServerConfig:
    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


BUILTIN_CONFIG = {"servers": {"internet-signals": {"command": "python", "args": ["-m", "upstream.server"]}}}
_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")


def load_server_configs(path: str | None = None, log: Callable[[str], None] = print) -> list[ServerConfig]:
    """Read servers.json (same shape as Claude Desktop's mcpServers, plus `requires_env`)."""
    candidate = Path(path or os.environ.get("UPSTREAM_SERVERS", "servers.json"))
    raw = json.loads(candidate.read_text()) if candidate.exists() else BUILTIN_CONFIG
    configs = []
    for name, spec in raw.get("servers", {}).items():
        missing = [var for var in spec.get("requires_env", []) if not os.environ.get(var)]
        if missing:
            log(f"  (skipping MCP server '{name}': set {', '.join(missing)} to enable it)")
            continue

        def expand(value: str) -> str:
            return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)

        command = spec.get("command")
        if command == "python":  # use this interpreter, so the venv's packages are visible
            command = sys.executable
        configs.append(
            ServerConfig(
                name=name,
                command=command,
                args=[expand(a) for a in spec.get("args", [])],
                env={k: expand(v) for k, v in spec.get("env", {}).items()},
                url=expand(spec["url"]) if spec.get("url") else None,
                headers={k: expand(v) for k, v in spec.get("headers", {}).items()},
            )
        )
    return configs


# --------------------------------------------------------------------------- #
# Run statistics: the "receipt" printed after every investigation
# --------------------------------------------------------------------------- #


@dataclass
class RunStats:
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    tools_used: Counter = field(default_factory=Counter)
    started: float = field(default_factory=time.perf_counter)
    seconds: float = 0.0

    def receipt(self) -> str:
        top = ", ".join(f"{name.split('__')[-1]}×{n}" for name, n in self.tools_used.most_common())
        return (
            f"{self.seconds:.1f}s · {self.llm_calls} model calls · {self.input_tokens:,} in / "
            f"{self.output_tokens:,} out tokens ({self.cache_read_tokens:,} cached) · "
            f"{self.tool_calls} tool calls, {self.tool_errors} errors\n  tools: {top or 'none'}"
        )


# --------------------------------------------------------------------------- #
# The agent
# --------------------------------------------------------------------------- #

Event = Callable[[str, dict[str, Any]], None]


def _quiet(_kind: str, _data: dict[str, Any]) -> None:
    pass


class TriageAgent:
    """Connects to MCP servers, exposes their tools to Claude, and runs the tool-use loop."""

    def __init__(
        self,
        servers: list[ServerConfig],
        model: str = DEFAULT_MODEL,
        max_turns: int = 12,
        on_event: Event = _quiet,
    ):
        self.servers = servers
        self.model = model
        self.max_turns = max_turns
        self.on_event = on_event
        self._clients: dict[str, Client] = {}
        self._tools: dict[str, tuple[str, str]] = {}  # qualified name -> (server, tool)
        self.tool_specs: list[dict[str, Any]] = []
        self._stack = AsyncExitStack()
        self._llm = None

    # -- connection ------------------------------------------------------- #

    async def __aenter__(self) -> TriageAgent:
        for cfg in self.servers:
            if cfg.url:
                http = httpx2.AsyncClient(headers=cfg.headers, timeout=60)
                self._stack.push_async_callback(http.aclose)
                transport: Any = streamable_http_client(cfg.url, http_client=http)
            else:
                # Pass our environment through so the server inherits OTEL_* settings.
                env = {**os.environ, **cfg.env}
                transport = StdioServerParameters(command=cfg.command, args=cfg.args, env=env)
            client = await self._stack.enter_async_context(Client(transport))
            self._clients[cfg.name] = client
            for tool in (await client.list_tools()).tools:
                qualified = f"{cfg.name}__{tool.name}"[:64]
                self._tools[qualified] = (cfg.name, tool.name)
                self.tool_specs.append(
                    {"name": qualified, "description": tool.description or "", "input_schema": tool.input_schema}
                )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._stack.aclose()

    def llm(self):
        if self._llm is None:
            from anthropic import AsyncAnthropic  # imported lazily so `upstream tools` works without a key

            self._llm = AsyncAnthropic()
        return self._llm

    # -- tools ------------------------------------------------------------ #

    async def call_tool(self, qualified: str, arguments: dict[str, Any], call_id: str = "", stats: RunStats | None = None):
        """Call one MCP tool. Returns (text, is_error). Records an execute_tool span."""
        server, tool = self._tools[qualified]
        stats = stats or RunStats()
        attrs = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": tool,
            "gen_ai.tool.call.id": call_id,
            "gen_ai.tool.type": "extension",
            "upstream.mcp.server": server,
        }
        if CAPTURE_CONTENT:
            attrs["gen_ai.tool.call.arguments"] = json.dumps(arguments)
        with tracer.start_as_current_span(f"execute_tool {tool}", kind=SpanKind.INTERNAL, attributes=attrs) as span:
            try:
                result = await self._clients[server].call_tool(tool, arguments)
                is_error = bool(result.is_error)
                text = "\n".join(b.text for b in result.content if getattr(b, "type", "") == "text")
                if not text and result.structured_content is not None:
                    text = json.dumps(result.structured_content)
            except Exception as exc:  # transport failures, timeouts, protocol errors
                is_error, text = True, f"Tool call failed: {type(exc).__name__}: {exc}"
                span.record_exception(exc)
            # The finding this demo is built around: an MCP tool error arrives as a *successful*
            # JSON-RPC response with isError=true, so transport-level spans stay green. Mark it here.
            if is_error:
                span.set_status(StatusCode.ERROR, text[:200])
                span.set_attribute("error.type", "tool_error")
                stats.tool_errors += 1
            if CAPTURE_CONTENT:
                span.set_attribute("gen_ai.tool.call.result", text[:4000])
            stats.tool_calls += 1
            stats.tools_used[qualified] += 1
            if len(text) > MAX_TOOL_RESULT_CHARS:
                text = text[:MAX_TOOL_RESULT_CHARS] + "\n…(truncated)"
            return text, is_error

    # -- model ------------------------------------------------------------ #

    async def _chat(self, stats: RunStats, **kwargs: Any):
        attrs = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "anthropic",
            "gen_ai.request.model": kwargs["model"],
            "gen_ai.request.max_tokens": kwargs["max_tokens"],
        }
        with tracer.start_as_current_span(f"chat {kwargs['model']}", kind=SpanKind.CLIENT, attributes=attrs) as span:
            resp = await self.llm().messages.create(**kwargs)
            usage = resp.usage
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            span.set_attributes(
                {
                    "gen_ai.response.id": resp.id,
                    "gen_ai.response.model": resp.model,
                    "gen_ai.response.finish_reasons": [resp.stop_reason or "unknown"],
                    "gen_ai.usage.input_tokens": usage.input_tokens,
                    "gen_ai.usage.output_tokens": usage.output_tokens,
                    "gen_ai.usage.cache_read.input_tokens": cache_read,
                }
            )
            stats.llm_calls += 1
            stats.input_tokens += usage.input_tokens
            stats.output_tokens += usage.output_tokens
            stats.cache_read_tokens += cache_read
            return resp

    # -- the loop --------------------------------------------------------- #

    async def investigate(self, question: str, now: str | None = None) -> tuple[str, RunStats]:
        stats = RunStats()
        current = now or iso(datetime.now(timezone.utc))
        system = SYSTEM_PROMPT + f"\nCurrent time: {current}\n" + (REPLAY_ADDENDUM.format(now=now) if now else "")
        # Cache the (large, unchanging) tool definitions + system prompt across turns.
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        answer = ""

        root_attrs = {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "upstream-triage",
            "gen_ai.request.model": self.model,
            "upstream.replay_now": now or "",
            "upstream.mcp.servers": list(self._clients),
        }
        with tracer.start_as_current_span("invoke_agent upstream-triage", attributes=root_attrs) as root:
            if CAPTURE_CONTENT:
                root.set_attribute("upstream.question", question)
            for turn in range(self.max_turns):
                last_turn = turn == self.max_turns - 1
                resp = await self._chat(
                    stats,
                    model=self.model,
                    max_tokens=4096,
                    system=system_blocks,
                    tools=self.tool_specs,
                    # On the final turn, force a written answer instead of more tool calls.
                    tool_choice={"type": "none"} if last_turn else {"type": "auto"},
                    messages=messages,
                )
                messages.append({"role": "assistant", "content": resp.content})
                text = "".join(b.text for b in resp.content if b.type == "text").strip()
                calls = [b for b in resp.content if b.type == "tool_use"]
                if not calls:
                    answer = text
                    break
                if text:
                    self.on_event("thought", {"text": text})
                for c in calls:
                    self.on_event("tool_call", {"name": c.name, "input": c.input})
                # Independent tool calls run concurrently; each gets its own span.
                results = await asyncio.gather(*(self.call_tool(c.name, c.input, c.id, stats) for c in calls))
                for c, (out, is_error) in zip(calls, results):
                    self.on_event("tool_result", {"name": c.name, "is_error": is_error, "chars": len(out)})
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": c.id, "content": out, "is_error": is_error}
                            for c, (out, is_error) in zip(calls, results)
                        ],
                    }
                )
            stats.seconds = time.perf_counter() - stats.started
            root.set_attributes(
                {
                    "gen_ai.usage.input_tokens": stats.input_tokens,
                    "gen_ai.usage.output_tokens": stats.output_tokens,
                    "upstream.llm_calls": stats.llm_calls,
                    "upstream.tool_calls": stats.tool_calls,
                    "upstream.tool_errors": stats.tool_errors,
                }
            )
            if CAPTURE_CONTENT:
                root.set_attribute("upstream.answer", answer[:4000])
        return answer, stats

    async def grade(self, answer: str, expect: str) -> dict[str, str]:
        """LLM-as-judge: compare an answer to a scenario's reference diagnosis."""
        with tracer.start_as_current_span("evaluate upstream-triage") as span:
            resp = await self._chat(
                RunStats(),
                model=self.model,
                max_tokens=300,
                messages=[{"role": "user", "content": JUDGE_PROMPT.format(expect=expect, answer=answer)}],
            )
            raw = "".join(b.text for b in resp.content if b.type == "text")
            match = re.search(r"\{.*\}", raw, re.S)
            try:
                result = json.loads(match.group(0)) if match else {}
            except json.JSONDecodeError:
                result = {}
            result = {"grade": result.get("grade", "fail"), "reason": result.get("reason", raw.strip()[:200])}
            span.set_attribute("upstream.eval.grade", result["grade"])
            return result
