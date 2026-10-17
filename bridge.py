"""Session-owned MCP bridge: give one Codex CLI lead persistent teammates
backed by any OpenAI-compatible endpoint you already run.

The lead (your official Codex session) talks to this process over stdio MCP.
On first use the bridge spawns ONE child `codex app-server` whose CODEX_HOME
points at a worker-only config: your own provider, never the lead's login.
Each teammate is a native Codex thread that keeps its context across turns.

Config: a TOML file (see team.config.example.toml), found via --config,
$CODEX_TEAM_CONFIG, or team.config.toml next to this file.
"""
import argparse
import atexit
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("codex-team needs Python 3.11+ (tomllib)")

DEFAULT_WORKER_INSTRUCTIONS = (
    "You are a persistent teammate. Stay within the assigned narrow task. "
    "Follow revisions. Do not delegate, and do not modify services or "
    "credentials. Writing files requires explicitly assigned implementation "
    "work. Never launch background services."
)

CFG = None          # parsed team config
MODELS = {}         # alias -> upstream model name
HOME = None         # worker state root
CLI = None          # path/command for the codex CLI

session = uuid.uuid4().hex
RUNTIME_HOME = None
workers = {}
pending = {}
lock = threading.RLock()
write_lock = threading.Lock()
seq = 0
proc = None
server = None
STATE = None


def load_config(path=None):
    """Resolve and parse the team config. Side-effect free."""
    global CFG, MODELS, HOME, CLI, RUNTIME_HOME, STATE
    if path is None:
        env = os.environ.get("CODEX_TEAM_CONFIG")
        path = Path(env) if env else Path(__file__).with_name("team.config.toml")
    path = Path(path)
    if not path.exists():
        sys.exit(
            f"team config not found: {path}\n"
            "Copy team.config.example.toml to team.config.toml and edit it, "
            "or pass --config / set CODEX_TEAM_CONFIG."
        )
    CFG = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    MODELS = dict(CFG.get("models", {}))
    if not MODELS:
        sys.exit(f"team config {path} has no [models] section")
    HOME = Path(os.path.expanduser(CFG.get("bridge", {}).get(
        "state_home", "~/.codex-team")))
    CLI = CFG.get("bridge", {}).get("codex_cli") or shutil.which("codex")
    if CLI is None:
        sys.exit("codex CLI not found on PATH; set bridge.codex_cli in the config")
    RUNTIME_HOME = HOME / "runs" / session
    STATE = HOME / "teams" / f"{session}.json"
    return CFG


def build_worker_toml(cfg):
    """Worker-only Codex config as a TOML string. Side effect free (testing)."""
    provider = cfg.get("provider", {})
    base_url = provider.get("base_url")
    if not base_url:
        sys.exit("team config requires provider.base_url")
    first_model = next(iter(MODELS.values()))
    lines = [
        f'model={json.dumps(first_model)}',
        'model_provider="team_workers"',
        'approval_policy="never"',
        'sandbox_mode="read-only"',
        "",
        "[agents]",
        "enabled=false",
        "",
        "[model_providers.team_workers]",
        f'name={json.dumps(provider.get("name", "team worker provider"))}',
        f'base_url={json.dumps(base_url)}',
        f'wire_api={json.dumps(provider.get("wire_api", "responses"))}',
    ]
    token_env = provider.get("bearer_token_env")
    if token_env:
        token = os.environ.get(token_env)
        if not token:
            sys.exit(f"provider.bearer_token_env names {token_env} but it is not set")
        lines.append(f"experimental_bearer_token={json.dumps(token)}")
    lines.append("requires_openai_auth=false")
    return "\n".join(lines) + "\n"


def save():
    with lock:
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"session": session, "workers": workers}, ensure_ascii=False, indent=2),
            encoding="utf8")
        tmp.replace(STATE)


def emit(method, params, ident=None):
    msg = {"method": method, "params": params}
    if ident is not None:
        msg["id"] = ident
    with write_lock:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()


def rpc(method, params):
    global seq
    with lock:
        seq += 1
        ident = seq
        q = queue.Queue()
        pending[ident] = q
    emit(method, params, ident)
    try:
        msg = q.get(timeout=45)
        if "error" in msg:
            raise RuntimeError(str(msg["error"]))
        return msg["result"]
    finally:
        with lock:
            pending.pop(ident, None)


def read_events():
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        ident = msg.get("id")
        with lock:
            if ident in pending:
                pending[ident].put(msg)
                continue
            par = msg.get("params", {})
            thread = par.get("threadId")
            w = next((w for w in workers.values() if w["thread"] == thread), None)
            if not w:
                continue
            method = msg.get("method")
            if method == "turn/started":
                w["turn"] = par["turn"]["id"]
                w["status"] = "running"
            elif method == "item/agentMessage/delta":
                w["output"] += par.get("delta", "")
            elif method == "turn/completed":
                turn = par["turn"]
                w["status"] = turn.get("status", "done")
                w["error"] = turn.get("error")
                w["turn"] = None
            else:
                continue
            save()


def ensure_host():
    global proc, server
    with lock:
        if proc:
            return
        HOME.mkdir(parents=True, exist_ok=True)
        RUNTIME_HOME.mkdir(parents=True, exist_ok=True)
        STATE.parent.mkdir(parents=True, exist_ok=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Dashboard)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        bus_url = f"http://127.0.0.1:{server.server_port}/{session}/"
        bus_path = str(Path(__file__).with_name("team_bus.py")).replace("\\", "/")
        bus_config = (
            '\n[mcp_servers.team_bus]\n'
            f'command={json.dumps(sys.executable)}\n'
            f'args={json.dumps([bus_path, bus_url])}\n'
            'default_tools_approval_mode="auto"\n'
        )
        (RUNTIME_HOME / "config.toml").write_text(
            build_worker_toml(CFG) + bus_config, encoding="utf8")
        scrub = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                 "CHATGPT_ACCESS_TOKEN"}
        worker_env = {k: v for k, v in os.environ.items() if k not in scrub}
        worker_env["CODEX_HOME"] = str(RUNTIME_HOME)
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [str(CLI), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf8",
            env=worker_env, **popen_kwargs)
        threading.Thread(target=read_events, daemon=True).start()
    rpc("initialize", {"clientInfo": {"name": "codex_team_bridge", "version": "1.0"},
                       "capabilities": {"experimentalApi": True}})
    emit("initialized", {})


def overview(name=None, full=True):
    with lock:
        selected = [w for w in workers.values() if name is None or w["name"] == name]
        rows = [{k: w.get(k) for k in
                 ["name", "model", "thread", "status", "output", "error"]}
                for w in selected]
        if not full:
            for row in rows:
                row["output"] = row["output"][-4000:]
        return {"session": session,
                "dashboard": (f"http://127.0.0.1:{server.server_port}/{session}/"
                              if server else None),
                "workers": rows}


def ack(name):
    result = overview(name, False)
    for row in result["workers"]:
        row.pop("output", None)
    return result


def start(name, model, task, cwd=None, workspace_write=False):
    if model not in MODELS:
        raise ValueError(f"unknown model alias {model!r}; configured: {sorted(MODELS)}")
    ensure_host()
    with lock:
        if name in workers:
            raise ValueError("Name already exists; use team_send for the same worker")
    instructions = CFG.get("bridge", {}).get(
        "worker_instructions", DEFAULT_WORKER_INSTRUCTIONS)
    result = rpc("thread/start", {
        "model": MODELS[model], "modelProvider": "team_workers",
        "cwd": cwd or os.getcwd(), "approvalPolicy": "never",
        "sandbox": "workspace-write" if workspace_write else "read-only",
        "developerInstructions": instructions})
    with lock:
        workers[name] = {"name": name, "model": MODELS[model],
                         "thread": result["thread"]["id"], "status": "idle",
                         "turn": None, "output": "", "error": None, "history": []}
        save()
    send(name, task)
    return ack(name)


def send(name, message):
    ensure_host()
    with lock:
        w = workers[name]
        thread = w["thread"]
        turn = w["turn"]
        w["history"].append({"role": "user", "text": message})
        if not turn:
            w["output"] = ""
            w["error"] = None
            w["status"] = "starting"
    if turn:
        rpc("turn/steer", {"threadId": thread, "expectedTurnId": turn,
                           "input": [{"type": "text", "text": message}]})
    else:
        result = rpc("turn/start", {"threadId": thread,
                                    "input": [{"type": "text", "text": message}]})
        with lock:
            if w["status"] == "starting":
                w["turn"] = result["turn"]["id"]
                w["status"] = "running"
    save()
    return ack(name)


def cancel(name):
    with lock:
        w = workers[name]
        turn = w["turn"]
        thread = w["thread"]
    if turn:
        rpc("turn/interrupt", {"threadId": thread, "turnId": turn})
    return ack(name)


HTML = r'''<!doctype html><meta charset="utf-8"><title>Codex Team</title><style>body{font:16px system-ui;background:#101522;color:#eef2ff;margin:24px}section{border:1px solid #53627c;padding:16px;margin:16px 0;border-radius:12px}pre{white-space:pre-wrap}textarea{width:95%;height:70px}button{padding:10px;margin:8px}</style><h1>Codex Team workers</h1><p>Messages go to the same teammate; cancel interrupts only that teammate. The lead agent drives this team from its own session.</p><main></main><script>
const base=location.pathname,main=document.querySelector('main'),panels=new Map();
async function action(name,op){const box=panels.get(name);const r=await fetch(base+'action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,op,message:box.querySelector('textarea').value})});if(!r.ok)alert((await r.json()).error);else box.querySelector('textarea').value='';}
async function refresh(){const s=await(await fetch(base+'state')).json();for(const w of s.workers){let box=panels.get(w.name);if(!box){box=document.createElement('section');box.innerHTML='<h2></h2><p></p><pre></pre><textarea placeholder="Follow up or replace this teammate&#39;s task"></textarea><br><button>Send</button><button>Cancel current turn</button>';box.querySelectorAll('button')[0].onclick=()=>action(w.name,'send');box.querySelectorAll('button')[1].onclick=()=>action(w.name,'cancel');panels.set(w.name,box);main.append(box);}box.querySelector('h2').textContent=w.name+' · '+w.model;box.querySelector('p').textContent=w.status;box.querySelector('pre').textContent=w.output+(w.error?'\n'+JSON.stringify(w.error):'');}}refresh();setInterval(refresh,1500);
</script>'''


class Dashboard(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, code, value, html=False):
        data = (value if html else json.dumps(value, ensure_ascii=False)).encode("utf8")
        self.send_response(code)
        self.send_header("Content-Type",
                         "text/html; charset=utf-8" if html else "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get("Host") != f"127.0.0.1:{server.server_port}":
            return self.respond(403, {"error": "Host rejected"})
        if self.path == f"/{session}/":
            return self.respond(200, HTML, True)
        if self.path == f"/{session}/state":
            return self.respond(200, overview())
        self.respond(404, {})

    def do_POST(self):
        expected = f"http://127.0.0.1:{server.server_port}"
        if (self.headers.get("Host") != expected.removeprefix("http://")
                or self.headers.get("Origin") != expected
                or self.path != f"/{session}/action"):
            return self.respond(403, {"error": "Origin rejected"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16000:
                raise ValueError("Message too long")
            data = json.loads(self.rfile.read(length))
            op = data["op"]
            result = (send(data["name"], data["message"]) if op == "send"
                      else cancel(data["name"]) if op == "cancel" else None)
            if result is None:
                raise ValueError("Unknown action")
            self.respond(200, result)
        except Exception as e:
            self.respond(400, {"error": str(e)})


def cleanup():
    if server:
        server.shutdown()
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


atexit.register(cleanup)


def tools_spec():
    tools = [
        {"name": "team_start",
         "description": "Create one persistent teammate on the configured worker provider. Returns immediately; use team_status for its result. workspace_write must only be true for authorized implementation work.",
         "inputSchema": {"type": "object", "properties": {
             "name": {"type": "string"},
             "model": {"type": "string", "enum": sorted(MODELS)},
             "task": {"type": "string"},
             "cwd": {"type": "string"},
             "workspace_write": {"type": "boolean", "default": False}},
             "required": ["name", "model", "task"]}},
        {"name": "team_send",
         "description": "Follow up with the SAME teammate; steer its running turn or start a new turn with retained context.",
         "inputSchema": {"type": "object", "properties": {
             "name": {"type": "string"}, "message": {"type": "string"}},
             "required": ["name", "message"]}},
        {"name": "team_status",
         "description": "Read bounded teammate results and the dashboard URL. Filter by name to save lead tokens. Does not resend worker context.",
         "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
        {"name": "team_cancel",
         "description": "Interrupt only this teammate's current turn; preserve its thread and all other teammates.",
         "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                         "required": ["name"]}}]
    for tool in tools:
        tool["annotations"] = {"readOnlyHint": False, "destructiveHint": False,
                               "openWorldHint": False,
                               "idempotentHint": tool["name"] == "team_status"}
    return tools


def dispatch(msg):
    method = msg.get("method")
    params = msg.get("params", {})
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "codex-team", "version": "1.0"}}
    if method == "tools/list":
        return {"tools": tools_spec()}
    if method == "tools/call":
        name = params["name"]
        args = params.get("arguments", {})
        try:
            result = (start(**args) if name == "team_start"
                      else send(**args) if name == "team_send"
                      else cancel(**args) if name == "team_cancel"
                      else overview(full=False, **args) if name == "team_status"
                      else None)
            if result is None:
                raise ValueError("Unknown tool")
            return {"content": [{"type": "text",
                                 "text": json.dumps(result, ensure_ascii=False)}]}
        except Exception as e:
            return {"isError": True, "content": [{"type": "text", "text": str(e)}]}
    if method == "ping":
        return {}
    raise ValueError("Unknown method")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="path to team.config.toml")
    args = parser.parse_args()
    load_config(args.config)
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" not in msg:
            continue
        try:
            response = {"jsonrpc": "2.0", "id": msg["id"], "result": dispatch(msg)}
        except Exception as e:
            response = {"jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32603, "message": str(e)}}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
