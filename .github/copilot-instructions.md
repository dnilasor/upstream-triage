# Upstream triage: project instructions

Upstream answers "is it us, or is it upstream?" during an outage. A hand-written Claude tool-use
loop (`src/upstream/agent.py`) calls tools on an MCP server over public internet data
(`src/upstream/server.py`), and every step is traced with OpenTelemetry. Python 3.11+, packaged
with hatchling, installed with `pip install -e '.[dev]'`.

## Layout

- `src/upstream/server.py`: the `internet-signals` MCP server. 9 read-only tools over RIPEstat (BGP),
  IODA (outage detection) and Atlassian Statuspage, plus one prompt and one resource.
- `src/upstream/agent.py`: `TriageAgent`. Connects to the servers in `servers.json`, runs the loop,
  records spans, grades answers (`grade`) for `upstream eval`.
- `src/upstream/cli.py`: `upstream ask | replay | eval | tools | call`.
- `src/upstream/scenarios.py`: historical incidents replayed with a fake "now".
- `src/upstream/web.py`: Starlette page used by the Render deployment.
- `src/upstream/telemetry.py`, `src/upstream/timeutil.py`: shared OTel setup and time parsing.
- `tests/`: `test_server.py` (canned API fixtures) and `test_agent.py` (scripted fake Claude).
- `docs/what-the-traces-showed.md`: findings log from the tracing work.

## Commands

- Tests: `pytest -q`. They need no network and no API key; keep it that way.
- Explore the server without a key: `upstream tools`, `upstream call <tool> '<json args>'`.
- Agent runs need `ANTHROPIC_API_KEY`: `upstream replay <id>`, `upstream ask "..."`, `upstream eval`.
- MCP server standalone: `upstream-signals` (stdio) or `upstream-signals --http` (Streamable HTTP on 8000).
- Traces: the devcontainer sets `OTEL_EXPORTER_OTLP_ENDPOINT` to Jaeger (UI on port 16686).
  Without that variable, all spans are no-ops; code must work identically either way.

## Libraries and APIs

- Use `httpx2` (not `httpx`) for HTTP; it is what `mcp` and `anthropic` depend on.
- MCP Python SDK v2: `MCPServer`, `@mcp.tool`, `ToolError`, `ToolAnnotations`, and `Client` for
  clients. Do not introduce v1 names such as `FastMCP` or `ClientSession`.
- `pyproject.toml` caps `mcp` and `anthropic` below their next major release on purpose. Bump the
  floor when upgrading; do not remove the caps.
- Keep the agent loop hand-written on the Anthropic SDK. Do not add an agent framework; the point
  of the project is that every step is visible and gets its own span.
- The default model comes from `UPSTREAM_MODEL` with a fallback in `agent.py`; change it there, not
  in call sites.

## Writing or changing MCP tools (`server.py`)

- Return a summary the model can read, not the raw API payload. Include baseline, worst point,
  percentage of baseline and timestamps. Run large payloads through `_trim`.
- Every time-series tool takes an explicit `start` and `end` and validates them with `_window`
  (respect the per-tool `max_days`). Format times with `iso` / `iso_ts` (UTC, minute precision).
- Raise `ToolError` with a message that tells the model how to fix its input. Never let a tool
  raise a bare exception; the agent counts `ToolError`s as `isError` results.
- Decorate tools with `annotations=READ_ONLY`. The server must stay read-only and key-free.
- Fetch through `get_json`, which adds the client span, the 60 s cache and the `User-Agent`.
  RIPEstat calls go through `ripestat` so `sourceapp` is always sent.
- Upstream APIs change shape without notice (IODA added non-numeric datasources in 2025).
  Summarize each series in its own try block and skip odd ones with a `note` instead of failing
  the whole call.
- Adding a tool: update the tool list in `LANDING` and the README table, add a canned response to
  `fake_get_json` in `tests/test_server.py`, and add a test that calls it through `Client(server.mcp)`.

## Tracing conventions

- Span names and attributes follow the OpenTelemetry GenAI semantic conventions:
  `invoke_agent ...`, `chat <model>`, `execute_tool <tool>`, `gen_ai.usage.*`. Project-specific
  attributes use the `upstream.` prefix.
- An MCP tool error is a successful JSON-RPC response with `isError: true`, so the SDK's transport
  span stays green. The agent's `execute_tool` span must set `StatusCode.ERROR` and
  `error.type=tool_error` in that case. `test_stdio_transport_span_stays_green_on_tool_error`
  guards this; do not weaken it.
- Prompts, questions and tool payloads are recorded only when
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`. Gate any new content attribute on
  `CAPTURE_CONTENT`.
- Each process calls `telemetry.setup("<service name>")` once at startup; the agent passes its
  environment to stdio servers so they export to the same backend.

## Agent and servers

- `servers.json` uses Claude Desktop's `mcpServers` shape plus `requires_env`. A server whose
  required variables are missing is skipped with a message, not an error.
- If connecting to one configured server fails after others are open, close the exit stack
  before re-raising. Leaving a stdio server open makes `asyncio.run` hang on shutdown and hides
  the real error from the user.
- The Cloudflare Radar remote server needs an API token with Radar read plus the `user:read` and
  `account:read` scopes; otherwise it answers 403 `insufficient_scope`.
- Tool results are capped at `MAX_TOOL_RESULT_CHARS`; keep summaries well under it.
- Replay mode passes `now`; the system prompt forbids relative times and live-only tools then.
  Scenario `expect` text is the hidden answer key: never send it to the agent, only to the judge.

## Tests

- Server tests monkeypatch `server.get_json` with `fake_get_json`; fixtures mirror real response
  shapes captured from the Iberian blackout and Optus incidents. Extend them, do not stub ad hoc.
- Agent tests drive `TriageAgent` with `FakeClaude` and an in-process `Client(server.mcp)`, and
  assert on spans from an `InMemorySpanExporter`. Follow that pattern for new loop behavior.
- Name tests `test_<behavior>` and run `python -m pytest tests/<file>` after editing one.

## Documentation

- README command examples must run as written. Update the README tool table, the scenario table
  and `docs/what-the-traces-showed.md` when tools, scenarios or tracing behavior change.
- Prose style: plain sentences, no marketing. Numbers go next to the command that produced them.
