# What the traces showed

Notes on what instrumenting an MCP-based agent revealed. Finding 1 is reproduced by the test
suite. The rest is a log to fill in from real runs; sections marked _TODO_ are placeholders,
not results.

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

## 2. Server-side summarization vs. raw payloads

_TODO: compare tokens per investigation with `ioda_signal_summary` against a version that returns
the raw IODA series. Use `upstream eval` before and after, and record input tokens and grades._

## 3. Where the time goes

_TODO: in Jaeger, sort `invoke_agent` traces by duration. How much is model time (`chat` spans)
vs. tool time (`execute_tool`) vs. upstream API time (`GET ...`)? Do the parallel tool calls
actually overlap?_

## 4. Redundant or wasted tool calls

_TODO: look for repeated calls with the same arguments, calls to live-only tools in replay
mode, and windows without a baseline. Did prompt changes reduce them? Check the
`upstream.cache_hit` attribute on server HTTP spans._

## 5. Prompt caching

_TODO: compare `gen_ai.usage.cache_read.input_tokens` across the `chat` spans of one investigation._
