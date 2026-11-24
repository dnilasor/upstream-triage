# What the traces showed

Notes on what instrumenting an MCP-based agent revealed. Finding 1 is reproduced by the test
suite and Finding 2 came from a real run. The rest is a log to fill in from real runs; sections
marked _TODO_ are placeholders, not results.

## 1. Failed tool calls look healthy at the transport layer

**What happens:** MCP has two kinds of failure. A *protocol* error (bad method, malformed request)
is a JSON-RPC error. A *tool* error (bad input, an upstream API down) is a normal, successful
JSON-RPC response whose result carries `isError: true`, so the model can read the message and recover.

**Why it matters for observability:** in the MCP Python SDK v2, the client's transport span
(`MCP send tools/call <tool>`) is marked as an error only for JSON-RPC errors. On the server side,
the SDK's `tools/call` span *does* check `isError`. So the same failure is red on the server and
green on the client. A dashboard built on client-side spans, or an APM that only sees the agent
process, reports 100% success while the agent quietly works around broken tools.

**What this project does:** the agent wraps every call in an `execute_tool` span that sets
`error.type=tool_error` when `isError` is true, and counts errors per investigation
(`upstream.tool_errors` on the root span, and in the receipt printed after each run).

**Evidence:** `tests/test_agent.py::test_stdio_transport_span_stays_green_on_tool_error` runs a
real stdio server, triggers a tool error, and asserts that the `execute_tool` span is ERROR while
its child transport span is not.

## 2. An upstream API changed shape, and only the agent-side spans noticed

**What happened:** IODA's raw signals endpoint began returning datasources beyond the three it
had for years (`bgp`, `ping-slash24`, `merit-nt`). The new ones (`ping-slash24-loss`,
`ping-slash24-latency`, `mozilla`, `gtr`) carry a list of per-region records at each point
instead of a number. `ioda_signal_summary` assumed numbers, so every call with `signal='all'`
failed, and the raw response also overflowed the agent's 12k-character tool-result cap.

**How it showed up:** exactly as Finding 1 predicts. Each failure was a tool error
(`isError: true`), so the `MCP send tools/call` spans were green. The agent's `execute_tool`
spans were ERROR, and `upstream.tool_errors` on the root span was 5 for a single replay.
Without that wrapper the agent would have looked healthy while reasoning from a tool that
never returned data.

**Fix:** skip non-numeric series with a note, summarize each series in its own try block, and
with `signal='all'` summarize only the three core signals and list the rest by name. The test
fixture now includes a list-of-dicts series captured from the live API.

**Evidence:** `upstream replay iberian-blackout` went from 11 tool calls / 5 errors / 64s to
8 tool calls / 0 errors / 33s.

## 3. Server-side summarization vs. raw payloads

_TODO: compare tokens per investigation with `ioda_signal_summary` against a version that returns
the raw IODA series. Use `upstream eval` before and after, and record input tokens and grades._

## 4. Where the time goes

_TODO: in Jaeger, sort `invoke_agent` traces by duration. How much is model time (`chat` spans)
vs. tool time (`execute_tool`) vs. upstream API time (`GET ...`)? Do the parallel tool calls
actually overlap?_

## 5. Redundant or wasted tool calls

_TODO: look for repeated calls with the same arguments, calls to live-only tools in replay
mode, and windows without a baseline. Did prompt changes reduce them? Check the
`upstream.cache_hit` attribute on server HTTP spans._

## 6. Prompt caching

_TODO: compare `gen_ai.usage.cache_read.input_tokens` across the `chat` spans of one investigation._
