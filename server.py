"""
ChronosAI — FastAPI Control Plane, Live LLM Agent Runner & Model Context Protocol (MCP) Server.
"""

from pathlib import Path
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
    description="Deterministic Time-Travel Debugger, SQLite SAVEPOINT Shadow Firewall, Semantic Vector Cache, Self-Healing Interceptor & MCP Server",
    version="0.4.0",
)

engine = ChronosEngine()
engine.sandbox_db.reset_seed_data()
live_agent = LiveLLMAgent(engine)
mcp_server = ChronosMCPServer(engine)
interceptor = ChronosToolInterceptor(engine, self_heal=True)

# Seed initial production traces
_initial_finops = run_finops_incident_scenario(engine, use_cache=True)
_initial_sre = run_sre_secops_scenario(engine)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class ForkRequest(BaseModel):
    target_step_index: int = 5
    patched_input: Dict[str, Any] = {
        "query": "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0 WHERE order_id = 'ORD-9921';",
        "database": "prod_ledger_primary",
        "amount_usd": 120.0,
    }


class FirewallInspectRequest(BaseModel):
    tool_name: str = "execute_sql"
    args: Dict[str, Any] = {"query": "DELETE FROM orders;"}


class SimulateScenarioRequest(BaseModel):
    scenario_type: str = "finops"


class LivePromptRequest(BaseModel):
    prompt: str
    provider: str = "auto"  # "auto", "gemini", "openai", "ollama"
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
        [r.to_dict() for r in engine.runs.values()],
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
    return resp or {"jsonrpc": "2.0", "status": "ok"}


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
    if req.scenario_type == "sre":
        run = run_sre_secops_scenario(engine)
    else:
        engine.sandbox_db.reset_seed_data()
        run = run_finops_incident_scenario(engine, use_cache=True)
    return {
        "run": run.to_dict(),
        "metrics": engine.get_global_metrics(),
    }


@app.post("/api/runs/{run_id}/fork")
def fork_run(run_id: str, req: ForkRequest):
    source_run = engine.runs.get(run_id)
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
                    "status": "REMEDIATED_SAFELY",
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
