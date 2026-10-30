"""
Chronos MCP Server — Local tool interface using MCP-style JSON-RPC over stdio/HTTP.
Can be run via stdio (`python -m chronos.mcp_server`) for Claude Desktop, Cursor & Antigravity,
or mounted directly at `POST /mcp` on the FastAPI Control Plane.
"""

import json
import sys
from typing import Dict, Any, Optional

from .engine import ChronosEngine
from .llm_agent import LiveLLMAgent


MCP_TOOLS = [
    {
        "name": "chronos_inspect_tool",
        "description": (
            "Inspects and simulates a candidate tool call (SQL, Bash, or Stripe payment) inside the "
            "Chronos ShadowFirewall & SQLite SAVEPOINT sandbox against the configured local ledger; external services are not executed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Name of the tool: execute_sql, run_bash, or issue_refund",
                },
                "args": {
                    "type": "object",
                    "description": "Tool arguments dictionary (e.g. {'query': 'UPDATE orders ...'})",
                },
            },
            "required": ["tool_name", "args"],
        },
    },
    {
        "name": "chronos_execute_safe_sql",
        "description": (
            "Executes a SQL query against the configured local SQLite ledger inside a protected SAVEPOINT. "
            "Automatically rolls back if more than 1 row is mutated or if WHERE is omitted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "SQL query to execute safely inside SAVEPOINT",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "chronos_run_agent_prompt",
        "description": "Runs a natural-language task through the Chronos Live Agent with sequential trace recording.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Natural language instruction for the agent",
                }
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "chronos_get_ledger",
        "description": "Returns the live state of the SQLite orders ledger (row counts, status distribution, and sample rows).",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


class ChronosMCPServer:
    """
    Handles MCP JSON-RPC 2.0 requests (`initialize`, `tools/list`, `tools/call`) over stdio or HTTP.
    """

    def __init__(self, engine: Optional[ChronosEngine] = None):
        self.engine = engine or ChronosEngine.from_environment()
        self.live_agent = LiveLLMAgent(self.engine)

    def handle_jsonrpc(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                or not isinstance(request.get("method"), str)
                or not isinstance(request.get("params", {}), dict)):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params") or {}

        # Notifications have no id and expect no response
        if method.startswith("notifications/"):
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "chronos-mcp-firewall",
                        "version": "0.5.0",
                    },
                },
            }

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": MCP_TOOLS},
            }

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                output = self._dispatch_tool(tool_name, arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(output, indent=2)}],
                        "isError": False,
                    },
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
                        "isError": True,
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def _dispatch_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        if tool_name == "chronos_inspect_tool":
            res = self.engine.firewall.inspect_tool_call(
                arguments.get("tool_name", "execute_sql"),
                arguments.get("args", {}),
                commit_if_safe=False,
            )
            return res.to_dict()

        if tool_name == "chronos_execute_safe_sql":
            run = self.engine.start_run("MCP-SQL", "Explicit local ledger SQL")
            step = self.engine.record_step(run, "TOOL_CALL", "execute_sql",
                {"query": arguments.get("query", "")}, lambda i, m: {}, {}, use_cache=False)
            if run.status == "RUNNING":
                run.status = "COMPLETED"
            self.engine.storage.save_run(run.to_dict())
            return {**step.policy_result, "run_id": run.run_id, "output": step.output_payload}

        if tool_name == "chronos_run_agent_prompt":
            run = self.live_agent.run_prompt(arguments.get("prompt", ""))
            return run.to_dict()

        if tool_name == "chronos_get_ledger":
            return self.engine.sandbox_db.get_ledger_summary(limit=10)

        raise ValueError(f"Unknown MCP tool: {tool_name}")

    def serve_stdio(self) -> None:
        """Runs the MCP JSON-RPC loop over standard input/output."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                resp = self.handle_jsonrpc(req)
                if resp is not None:
                    sys.stdout.write(json.dumps(resp) + "\n")
                    sys.stdout.flush()
            except Exception as exc:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
                sys.stdout.write(json.dumps(err_resp) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    ChronosMCPServer().serve_stdio()
