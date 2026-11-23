"""Command line: `upstream ask | replay | eval | tools | call`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from upstream import telemetry
from upstream.agent import DEFAULT_MODEL, TriageAgent, load_server_configs
from upstream.scenarios import SCENARIOS

DIM, BOLD, RED, GREEN, YELLOW, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    DIM = BOLD = RED = GREEN = YELLOW = RESET = ""


def _print_event(kind: str, data: dict) -> None:
    if kind == "thought":
        print(f"{DIM}{data['text']}{RESET}")
    elif kind == "tool_call":
        args = ", ".join(f"{k}={v!r}" for k, v in data["input"].items())
        print(f"  {DIM}→ {data['name'].split('__')[-1]}({args}){RESET}")
    elif kind == "tool_result" and data["is_error"]:
        print(f"  {RED}✗ {data['name'].split('__')[-1]} returned an error{RESET}")


def _need_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ANTHROPIC_API_KEY is not set. Get a key at https://console.anthropic.com/ and export it.\n"
            "(You can still explore without one: try `upstream tools` or `upstream call`.)"
        )


async def _run(question: str, args, now: str | None = None) -> None:
    async with TriageAgent(load_server_configs(args.servers), model=args.model, on_event=_print_event) as agent:
        answer, stats = await agent.investigate(question, now=now)
    print(f"\n{answer}\n")
    print(f"{DIM}── receipt: {stats.receipt()}{RESET}")


async def cmd_ask(args) -> None:
    _need_key()
    await _run(args.question, args)


async def cmd_replay(args) -> None:
    if not args.scenario:
        print("Replay scenarios (run one with `upstream replay <id>`):\n")
        for s in SCENARIOS.values():
            print(f"  {BOLD}{s.id:<18}{RESET} {s.title}")
        return
    if args.scenario not in SCENARIOS:
        sys.exit(f"Unknown scenario {args.scenario!r}. Run `upstream replay` to list them.")
    _need_key()
    s = SCENARIOS[args.scenario]
    print(f"{BOLD}Replaying: {s.title}{RESET}\n{DIM}It is {s.now}. The page says:{RESET}\n> {s.prompt}\n")
    await _run(s.prompt, args, now=s.now)
    if args.show_answer:
        print(f"\n{YELLOW}Reference diagnosis:{RESET} {s.expect}")


async def cmd_eval(args) -> None:
    _need_key()
    ids = args.scenarios or list(SCENARIOS)
    unknown = [i for i in ids if i not in SCENARIOS]
    if unknown:
        sys.exit(f"Unknown scenario(s): {', '.join(unknown)}. Run `upstream replay` to list them.")
    rows = []
    async with TriageAgent(load_server_configs(args.servers), model=args.model) as agent:
        for sid in ids:
            s = SCENARIOS[sid]
            print(f"{DIM}running {sid}…{RESET}", flush=True)
            answer, stats = await agent.investigate(s.prompt, now=s.now)
            grade = await agent.grade(answer, s.expect)
            rows.append((sid, grade, stats))
    print(f"\n{'scenario':<18} {'grade':<8} {'secs':>5} {'tools':>5} {'errs':>4} {'tokens in/out':>15}  reason")
    for sid, grade, st in rows:
        color = {"pass": GREEN, "partial": YELLOW}.get(grade["grade"], RED)
        print(
            f"{sid:<18} {color}{grade['grade']:<8}{RESET} {st.seconds:>5.0f} {st.tool_calls:>5} {st.tool_errors:>4} "
            f"{st.input_tokens:>8,}/{st.output_tokens:<6,}  {DIM}{grade['reason']}{RESET}"
        )


async def cmd_tools(args) -> None:
    async with TriageAgent(load_server_configs(args.servers)) as agent:
        for spec in agent.tool_specs:
            server, _, tool = spec["name"].partition("__")
            first_line = spec["description"].strip().splitlines()[0] if spec["description"] else ""
            print(f"  {BOLD}{tool}{RESET} {DIM}[{server}]{RESET}\n    {first_line}")


async def cmd_call(args) -> None:
    """Call one tool directly, no model involved. Handy for exploring the data."""
    try:
        arguments = json.loads(args.arguments)
    except json.JSONDecodeError as exc:
        sys.exit(f"Arguments must be JSON, e.g. '{{\"resource\": \"AS13335\"}}' ({exc})")
    async with TriageAgent(load_server_configs(args.servers)) as agent:
        matches = [n for n in agent._tools if n == args.tool or n.endswith(f"__{args.tool}")]
        if not matches:
            sys.exit(f"No tool named {args.tool!r}. Run `upstream tools`.")
        text, is_error = await agent.call_tool(matches[0], arguments)
    try:
        text = json.dumps(json.loads(text), indent=2)
    except ValueError:
        pass
    print(f"{RED}{text}{RESET}" if is_error else text)


def main() -> None:
    parser = argparse.ArgumentParser(prog="upstream", description="Is it us, or is it upstream?")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model (default {DEFAULT_MODEL})")
    parser.add_argument("--servers", default=None, help="path to servers.json (default ./servers.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ask", help="investigate a live incident")
    p.add_argument("question")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("replay", help="investigate a historical incident as if it were happening now")
    p.add_argument("scenario", nargs="?")
    p.add_argument("--show-answer", action="store_true", help="print the reference diagnosis afterwards")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("eval", help="replay scenarios and grade the agent with an LLM judge")
    p.add_argument("scenarios", nargs="*", help="scenario ids (default: all)")
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("tools", help="list tools from the configured MCP servers (no API key needed)")
    p.set_defaults(fn=cmd_tools)

    p = sub.add_parser("call", help="call one tool directly, without the model (no API key needed)")
    p.add_argument("tool")
    p.add_argument("arguments", nargs="?", default="{}")
    p.set_defaults(fn=cmd_call)

    args = parser.parse_args()
    telemetry.setup("upstream-agent")
    try:
        asyncio.run(args.fn(args))
    except KeyboardInterrupt:
        pass
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    main()
