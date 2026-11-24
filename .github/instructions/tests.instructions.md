---
applyTo: 'tests/**/*.py'
description: Conventions for the pytest suite (no network, no API key, canned fixtures, in-process MCP client).
---

# Test conventions

- Tests must pass offline with no `ANTHROPIC_API_KEY`. Never call a real API or the real model.
- Stub outbound HTTP by monkeypatching `server.get_json` with `fake_get_json` from
  `tests/test_server.py`. Add new canned responses there, matched on a URL fragment, and keep them
  shaped like a real response from the source (note the capture date in a comment).
- Call tools through `Client(server.mcp)` (in-process), reading `result.is_error` and the text
  content, as the `call` helper in `test_server.py` does.
- For agent-loop behavior, subclass `TriageAgent` as `InProcAgent` does, script the model with
  `FakeClaude`, and assert on spans from the module-level `InMemorySpanExporter`
  (`EXPORTER.get_finished_spans()`), which the autouse fixture clears per test.
- The one real-subprocess test, `test_stdio_transport_span_stays_green_on_tool_error`, spawns the
  server over stdio with `sys.executable`. Keep it; it reproduces the project's main finding.
- Name tests `test_<behavior>` and run `python -m pytest tests/<file> -q` after editing a file.
