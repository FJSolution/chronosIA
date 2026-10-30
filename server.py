"""
ChronosAI — FastAPI Control Plane, Live LLM Agent Runner & Model Context Protocol (MCP) Server.
"""

import os
import secrets
from pathlib import Path
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal

from chronos.engine import ChronosEngine
from chronos.llm_agent import LiveLLMAgent
from chronos.mcp_server import ChronosMCPServer, MCP_TOOLS
from chronos.integrations import ChronosToolInterceptor, INTEGRATION_SNIPPETS
from chronos.scenarios import (
    run_finops_incident_scenario,
    continue_forked_finops_run,
    run_sre_secops_scenario,
)

app = FastAPI(
    title="ChronosAI Control Plane & MCP Server",
    description="Local ledger policies, execution traces and explicit demonstration scenarios",
    version="0.5.0",
)

engine = ChronosEngine.from_environment()
live_agent = LiveLLMAgent(engine)
mcp_server = ChronosMCPServer(engine)
interceptor = ChronosToolInterceptor(engine, self_heal=True, local_sql=True)


@app.middleware("http")
async def authenticate(request: Request, call_next):
    if request.url.path.startswith(("/api/", "/mcp", "/docs", "/redoc", "/openapi")):
        expected = os.environ.get("CHRONOS_API_TOKEN", "")
        if len(expected) < 32:
            return JSONResponse({"detail": "Configure CHRONOS_API_TOKEN with at least 32 characters"}, status_code=503)
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        admin = scheme.lower() == "bearer" and secrets.compare_digest(supplied.encode(), expected.encode())
        reader_token = os.environ.get("CHRONOS_READ_TOKEN", "")
        reader = scheme.lower() == "bearer" and len(reader_token) >= 32 and secrets.compare_digest(supplied.encode(), reader_token.encode())
        if not admin and not reader:
            return JSONResponse({"detail": "Authentication required"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        if not admin and request.method not in ("GET", "HEAD"):
            return JSONResponse({"detail": "Read-only token cannot mutate state"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return response


@app.exception_handler(ValueError)
async def invalid_operation(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(PermissionError)
async def forbidden_operation(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=403)


def require_demo():
    if not engine.demo_mode:
        raise HTTPException(status_code=403, detail="This action requires explicit demo mode")


STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class ForkRequest(BaseModel):
    target_step_index: int = Field(default=5, ge=1)
    patched_input: Dict[str, Any] = {
        "query": "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0 WHERE order_id = 'ORD-9921';",
        "database": "ledger",
        "amount_usd": 120.0,
    }


class FirewallInspectRequest(BaseModel):
    tool_name: str = "execute_sql"
    args: Dict[str, Any] = {"query": "DELETE FROM orders;"}


class SimulateScenarioRequest(BaseModel):
    scenario_type: Literal["finops", "sre"] = "finops"


class LivePromptRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=10000)
    provider: Literal["auto", "builtin", "gemini", "openai", "ollama"] = "auto"  # "auto", "gemini", "openai", "ollama"
    api_key: Optional[str] = None


@app.get("/", response_class=HTMLResponse)
def serve_studio():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>ChronosAI Studio loading...</h1>")
    return FileResponse(str(index_path))


@app.get("/api/overview")
def get_overview():
    runs_sorted = sorted(
        [r.to_dict() for r in list(engine.runs.values())],
        key=lambda x: x["created_at"],
        reverse=True,
    )
    return {
        "metrics": engine.get_global_metrics(),
        "runs": runs_sorted,
    }


@app.post("/api/agent/run")
def run_live_agent_prompt(req: LivePromptRequest):
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    run = live_agent.run_prompt(
        user_prompt=req.prompt.strip(),
        provider=req.provider,
        api_key=req.api_key,
    )
    return {
        "run": run.to_dict(),
        "metrics": engine.get_global_metrics(),
    }


@app.post("/api/agent/self-heal")
def run_self_healing_agent(req: SimulateScenarioRequest):
    require_demo()
    run = interceptor.run_self_healing_demo(scenario_type=req.scenario_type)
    return {
        "run": run.to_dict(),
        "metrics": engine.get_global_metrics(),
    }


@app.get("/api/integrations/info")
def get_integrations_info():
    return {
        "snippets": INTEGRATION_SNIPPETS,
        "cache_stats": engine.cache.stats(),
    }


@app.post("/mcp")
def handle_mcp_jsonrpc(payload: Dict[str, Any]):
    resp = mcp_server.handle_jsonrpc(payload)
    return resp if resp is not None else Response(status_code=204)


@app.get("/api/mcp/info")
def get_mcp_info():
    repo_dir = str(Path(__file__).parent.resolve()).replace("\\", "/")
    return {
        "protocol_version": "2024-11-05",
        "server_name": "chronos-mcp-firewall",
        "tools": MCP_TOOLS,
        "mcp_config_snippet": {
            "mcpServers": {
                "chronos-firewall": {
                    "command": "python",
                    "args": ["-m", "chronos.mcp_server"],
                    "cwd": repo_dir,
                }
            }
        },
    }


@app.get("/api/ledger")
def get_ledger():
    return engine.sandbox_db.get_ledger_summary(limit=15)


@app.post("/api/ledger/reset")
def reset_ledger():
    require_demo()
    engine.cache.clear()
    engine.sandbox_db.reset_seed_data()
    return {
        "status": "RESET_COMPLETE",
        "ledger": engine.sandbox_db.get_ledger_summary(limit=15),
    }


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = engine.runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return run.to_dict()


@app.get("/api/runs/{run_id}/export")
def export_run_bundle(run_id: str):
    run = engine.runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    bundle = engine.storage.export_chronos_bundle(run.to_dict())
    return JSONResponse(
        content=bundle,
        headers={"Content-Disposition": f'attachment; filename="{run_id}.chronos.json"'},
    )


@app.post("/api/runs/simulate")
def simulate_run(req: SimulateScenarioRequest):
    require_demo()
    if req.scenario_type == "sre":
        run = run_sre_secops_scenario(engine)
    else:
        run = run_finops_incident_scenario(engine, use_cache=True)
    return {
        "run": run.to_dict(),
        "metrics": engine.get_global_metrics(),
    }


@app.post("/api/runs/{run_id}/fork")
def fork_run(run_id: str, req: ForkRequest):
    require_demo()
    source_run = engine.runs.get(run_id)
    if source_run and source_run.execution_mode != "DEMO":
        raise HTTPException(status_code=400, detail="Generic runs require an explicit SDK continuation")
    if not source_run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    def _continuation(eng: ChronosEngine, forked_run, patched_inp: Dict[str, Any], mem: Dict[str, Any]):
        if "K8s-SecOps" in source_run.agent_name or "cmd" in patched_inp:
            cmd = patched_inp.get(
                "cmd",
                "logrotate -f /etc/logrotate.d/containers && journalctl --vacuum-size=500M",
            )
            eng.record_step(
                run=forked_run,
                step_type="TOOL_CALL",
                name="run_bash",
                input_payload={"cmd": cmd, "host": "ip-10-0-42-19.ec2.internal"},
                executor_fn=lambda i, m: {
                    "exit_code": 0,
                    "freed_gb": 54.8,
                    "new_disk_usage_pct": 38.4,
                    "status": "SIMULATED",
                    "executed": False,
                },
                memory_state=mem,
                estimated_tokens=290,
                estimated_cost_usd=0.0043,
                estimated_latency_ms=140,
                use_cache=False,
            )
        else:
            continue_forked_finops_run(eng, forked_run, patched_inp, mem)

    try:
        forked = engine.fork_from_step(
            source_run_id=run_id,
            target_step_index=req.target_step_index,
            patched_input=req.patched_input,
            continuation_fn=_continuation,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "forked_run": forked.to_dict(),
        "metrics": engine.get_global_metrics(),
    }


@app.post("/api/firewall/inspect")
def inspect_firewall_payload(req: FirewallInspectRequest):
    res = engine.firewall.inspect_tool_call(req.tool_name, req.args, commit_if_safe=False)
    return {
        "result": res.to_dict(),
        "metrics": engine.get_global_metrics(),
    }
