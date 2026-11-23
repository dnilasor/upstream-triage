"""A tiny web page for the agent, so people can try it from a link (e.g. on Render).

It runs the same TriageAgent as the CLI. Because every investigation spends YOUR
Anthropic credits, it has two guards: an optional passcode (DEMO_PASSCODE) and a
daily cap on investigations (DAILY_LIMIT, default 25).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import os

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from upstream import telemetry
from upstream.agent import TriageAgent, load_server_configs
from upstream.scenarios import SCENARIOS

DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "25"))
PASSCODE = os.environ.get("DEMO_PASSCODE", "")
_running = asyncio.Semaphore(2)
_usage = {"day": "", "count": 0}


def _take_quota() -> bool:
    today = dt.date.today().isoformat()
    if _usage["day"] != today:
        _usage.update(day=today, count=0)
    if _usage["count"] >= DAILY_LIMIT:
        return False
    _usage["count"] += 1
    return True


async def investigate(request: Request) -> JSONResponse:
    body = await request.json()
    if PASSCODE and body.get("passcode") != PASSCODE:
        return JSONResponse({"error": "Wrong or missing passcode."}, status_code=401)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return JSONResponse({"error": "This deployment has no ANTHROPIC_API_KEY configured."}, status_code=503)

    scenario = SCENARIOS.get(body.get("scenario") or "")
    question = scenario.prompt if scenario else str(body.get("question", "")).strip()[:600]
    if not question:
        return JSONResponse({"error": "Pick a scenario or type a question."}, status_code=400)
    if not _take_quota():
        return JSONResponse({"error": f"Daily demo limit ({DAILY_LIMIT}) reached. Try tomorrow."}, status_code=429)

    events: list[dict] = []

    def record(kind: str, data: dict) -> None:
        if kind == "tool_call":
            events.append({"tool": data["name"].split("__")[-1], "input": data["input"]})
        elif kind == "tool_result" and data["is_error"]:
            events.append({"error": data["name"].split("__")[-1]})

    async with _running:
        try:
            async with TriageAgent(load_server_configs(log=lambda _: None), on_event=record) as agent:
                answer, stats = await agent.investigate(question, now=scenario.now if scenario else None)
        except Exception as exc:  # surface a readable error rather than a bare 500
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
    return JSONResponse(
        {
            "answer": answer,
            "events": events,
            "receipt": stats.receipt(),
            "reference": scenario.expect if scenario else None,
        }
    )


async def index(_request: Request) -> HTMLResponse:
    options = "".join(f'<option value="{s.id}">{s.title}</option>' for s in SCENARIOS.values())
    return HTMLResponse(
        PAGE.replace("{{options}}", options).replace("{{passcode}}", "" if not PASSCODE else PASSCODE_FIELD)
    )


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


PASSCODE_FIELD = '<input id="passcode" type="password" placeholder="demo passcode">'

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upstream: is it us, or is it upstream?</title>
<style>
 :root{--fg:#1b1f24;--muted:#5b6470;--line:#e3e6ea;--accent:#2f6f4f;--bg:#fbfbf9;--err:#b42318}
 *{box-sizing:border-box} body{margin:0;font:16px/1.55 ui-sans-serif,system-ui,-apple-system,sans-serif;color:var(--fg);background:var(--bg)}
 main{max-width:760px;margin:0 auto;padding:48px 20px 80px}
 h1{font-size:28px;margin:0 0 4px;letter-spacing:-.01em} .sub{color:var(--muted);margin:0 0 28px}
 label{display:block;font-weight:600;margin:18px 0 6px} select,textarea,input{width:100%;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:#fff}
 textarea{min-height:84px;resize:vertical} .or{color:var(--muted);text-align:center;margin:10px 0 -6px;font-size:14px}
 button{margin-top:18px;font:inherit;font-weight:600;padding:10px 18px;border:0;border-radius:8px;background:var(--accent);color:#fff;cursor:pointer}
 button:disabled{opacity:.5;cursor:wait} #passcode{margin-top:18px}
 #log{margin-top:28px;font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted)} #log .e{color:var(--err)}
 #answer{margin-top:20px;padding:20px 22px;background:#fff;border:1px solid var(--line);border-radius:10px;display:none}
 #answer ul{padding-left:20px;margin:6px 0 12px} #answer p{margin:8px 0}
 .receipt{margin-top:14px;font-size:13px;color:var(--muted)} .ref{margin-top:16px;padding:12px 14px;background:#f3f6f2;border-radius:8px;font-size:14px}
 footer{margin-top:40px;font-size:13px;color:var(--muted)} a{color:var(--accent)}
</style></head><body><main>
<h1>Is it us, or is it upstream?</h1>
<p class="sub">An incident-triage agent built on Claude, MCP and public internet data (RIPEstat BGP, IODA outage detection,
status pages). Replay a real past incident, or describe a live one.</p>
<label for="scenario">Replay a historical incident</label>
<select id="scenario"><option value="">Choose one…</option>{{options}}</select>
<p class="or">or</p>
<label for="question">Describe a live incident</label>
<textarea id="question" placeholder="e.g. Users in Brazil say our site is slow since 14:00 UTC. Our origin is in us-east. Is it us?"></textarea>
{{passcode}}
<button id="go">Investigate</button>
<div id="log"></div><div id="answer"></div>
<footer>Each run takes 20–60 seconds. Source code and tracing setup: see the GitHub repo.</footer>
</main><script>
const $=id=>document.getElementById(id);
function md(s){const esc=s.replace(/&/g,'&amp;').replace(/</g,'&lt;');let html='',list=false;
 for(const line of esc.split('\\n')){const b=line.replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>').replace(/`(.+?)`/g,'<code>$1</code>');
  if(/^\\s*[-*] /.test(line)){if(!list){html+='<ul>';list=true}html+='<li>'+b.replace(/^\\s*[-*] /,'')+'</li>'}
  else{if(list){html+='</ul>';list=false}if(line.trim())html+='<p>'+b+'</p>'}}
 return html+(list?'</ul>':'')}
$('go').onclick=async()=>{
 const body={scenario:$('scenario').value,question:$('question').value,passcode:$('passcode')?.value};
 $('go').disabled=true;$('answer').style.display='none';$('log').textContent='Investigating… (the model is calling tools; this takes a bit)';
 try{const r=await fetch('/api/investigate',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json(); if(d.error){$('log').innerHTML='<span class="e">'+d.error+'</span>';return}
  $('log').innerHTML=d.events.map(e=>e.error?'<div class="e">✗ '+e.error+' returned an error</div>'
   :'<div>→ '+e.tool+'('+Object.entries(e.input).map(([k,v])=>k+'='+JSON.stringify(v)).join(', ')+')</div>').join('');
  $('answer').innerHTML=md(d.answer)+'<div class="receipt">'+d.receipt.replace('\\n','<br>')+'</div>'
   +(d.reference?'<div class="ref"><b>What actually happened:</b> '+d.reference+'</div>':'');
  $('answer').style.display='block';
 }catch(e){$('log').innerHTML='<span class="e">'+e+'</span>'}finally{$('go').disabled=false}};
</script></body></html>"""

@contextlib.asynccontextmanager
async def _lifespan(_app):
    telemetry.setup("upstream-web")
    yield
    telemetry.shutdown()


app = Starlette(
    routes=[
        Route("/", index),
        Route("/health", health),
        Route("/api/investigate", investigate, methods=["POST"]),
    ],
    lifespan=_lifespan,
)


def main() -> None:
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
