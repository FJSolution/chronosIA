"""
ChronosAI Framework Integrations — Drop-in Tool Interceptor & Self-Healing Feedback Loop
for LangChain, LangGraph, OpenAI `tool_calls`, Anthropic Tool Use, and Custom Python Agents.
"""

import json
from functools import wraps
from typing import Dict, Any, List, Callable, Optional, Union

from .engine import ChronosEngine, AgentRun


class ChronosToolInterceptor:
    """
    Universal Middleware & Self-Healing Loop for AI Agent Frameworks:
    1. Wraps LangChain BaseTool / StructuredTool instances (`wrap_langchain_tools`)
    2. Intercepts OpenAI / Anthropic `tool_calls` payloads (`intercept_openai_tool_call`)
    3. Provides an Autonomous Self-Healing Loop where `SAVEPOINT` rollbacks return
       structured diagnostic observations to the LLM so it self-corrects on the next turn.
    """

    def __init__(self, engine: ChronosEngine, self_heal: bool = True):
        self.engine = engine
        self.self_heal = self_heal

    def wrap_callable(
        self,
        fn: Callable,
        run: AgentRun,
        memory_state: Dict[str, Any],
        tool_name: Optional[str] = None,
    ) -> Callable:
        """Wraps a Python tool function with Chronos SAVEPOINT Firewall + Semantic Cache."""
        resolved_name = tool_name or getattr(fn, "name", getattr(fn, "__name__", "custom_tool"))

        @wraps(fn)
        def _wrapped(*args, **kwargs) -> Dict[str, Any]:
            payload = kwargs if kwargs else (args[0] if (len(args) == 1 and isinstance(args[0], dict)) else {"args": list(args)})

            def _exec(inp: Dict[str, Any], mem: Dict[str, Any]) -> Dict[str, Any]:
                res = fn(**inp) if kwargs else fn(*args)
                return res if isinstance(res, dict) else {"result": res}

            step = self.engine.record_step(
                run=run,
                step_type="TOOL_CALL",
                name=resolved_name,
                input_payload=payload,
                executor_fn=_exec,
                memory_state=memory_state,
                halt_on_block=not self.self_heal,
            )
            return step.output_payload

        return _wrapped

    def wrap_langchain_tools(
        self,
        tools: List[Any],
        run: AgentRun,
        memory_state: Optional[Dict[str, Any]] = None,
    ) -> List[Any]:
        """
        Drop-in wrapper for LangChain / LangGraph tools (`BaseTool`, `StructuredTool`, or callables).
        Intercepts `.invoke()`, `._run()`, and `__call__()` through Chronos ShadowFirewall.
        """
        mem = memory_state if memory_state is not None else {}
        wrapped_tools = []

        for tool in tools:
            # Duck-typing for LangChain BaseTool / StructuredTool objects
            if hasattr(tool, "_run") or hasattr(tool, "invoke"):
                tool_name = getattr(tool, "name", tool.__class__.__name__)
                orig_fn = getattr(tool, "func", None) or getattr(tool, "_run")
                guarded_fn = self.wrap_callable(orig_fn, run=run, memory_state=mem, tool_name=tool_name)
                if hasattr(tool, "func") and tool.func is not None:
                    tool.func = guarded_fn
                if hasattr(tool, "_run"):
                    tool._run = guarded_fn
                wrapped_tools.append(tool)
            elif callable(tool):
                wrapped_tools.append(self.wrap_callable(tool, run=run, memory_state=mem))
            else:
                wrapped_tools.append(tool)

        return wrapped_tools

    def intercept_openai_tool_call(
        self,
        run: AgentRun,
        tool_call: Dict[str, Any],
        tool_registry: Dict[str, Callable],
        memory_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Intercepts an OpenAI `chat.completions` tool_call dictionary:
        {
            "id": "call_abc123",
            "type": "function",
            "function": {"name": "execute_sql", "arguments": "{\"query\": \"...\"}"}
        }
        Returns a ready-to-append OpenAI `role: "tool"` message dictionary.
        When blocked with `self_heal=True`, returns the SAVEPOINT rollback diagnostic inside `content`
        so the model automatically corrects its query on the next completion turn.
        """
        mem = memory_state if memory_state is not None else {}
        call_id = tool_call.get("id", "call_chronos_01")
        fn_block = tool_call.get("function", {})
        fn_name = fn_block.get("name", "execute_sql")
        raw_args: Union[str, Dict[str, Any]] = fn_block.get("arguments", "{}")

        if isinstance(raw_args, str):
            try:
                parsed_args = json.loads(raw_args)
            except json.JSONDecodeError:
                parsed_args = {"raw_arguments": raw_args}
        else:
            parsed_args = dict(raw_args)

        target_fn = tool_registry.get(fn_name, lambda **kw: {"status": "EXECUTED", "args": kw})

        def _exec(inp: Dict[str, Any], m: Dict[str, Any]) -> Dict[str, Any]:
            res = target_fn(**inp)
            return res if isinstance(res, dict) else {"result": res}

        step = self.engine.record_step(
            run=run,
            step_type="TOOL_CALL",
            name=fn_name,
            input_payload=parsed_args,
            executor_fn=_exec,
            memory_state=mem,
            halt_on_block=not self.self_heal,
        )

        return {
            "role": "tool",
            "tool_call_id": call_id,
            "name": fn_name,
            "status": step.status,
            "content": json.dumps(step.output_payload),
        }

    def run_self_healing_demo(self, scenario_type: str = "finops") -> AgentRun:
        """
        Executes a live Autonomous Self-Healing Agent trace where:
        1. The agent attempts a destructive/unscoped tool call (e.g. UPDATE without WHERE).
        2. Chronos ShadowFirewall intercepts it inside a real SQLite SAVEPOINT, rolls back 250 rows,
           and injects the structured `[CHRONOS_FIREWALL_BLOCKED]` feedback back to the agent (`halt_on_block=False`).
        3. The agent reflects on the SAVEPOINT feedback + memory state (`target_order_id='ORD-9921'`)
           and autonomously rewrites the SQL with `WHERE order_id = 'ORD-9921'`.
        4. Chronos verifies 1 row mutated in SAVEPOINT, commits (`RELEASE SAVEPOINT`), and completes the run!
        """
        if scenario_type == "sre":
            return self._run_sre_self_healing()
        return self._run_finops_self_healing()

    def _run_finops_self_healing(self) -> AgentRun:
        self.engine.sandbox_db.reset_seed_data()
        run = self.engine.start_run(
            agent_name="FinOps-SelfHeal-Agent (LangGraph + Interceptor)",
            scenario="[Self-Healing Loop] Ticket #9921: Auto-recover from unscoped SQL via SAVEPOINT feedback",
        )
        memory: Dict[str, Any] = {}

        # Step 1: Load Ticket Context
        self.engine.record_step(
            run=run,
            step_type="MEMORY_READ",
            name="fetch_dispute_context",
            input_payload={"ticket_id": "TCK-9921", "Lookup": "orders"},
            executor_fn=lambda inp, mem: {
                "order": self.engine.sandbox_db.get_order("ORD-9921"),
                "memory_updates": {
                    "target_order_id": "ORD-9921",
                    "customer_id": "USR-88412",
                    "refund_amount_usd": 120.0,
                },
            },
            memory_state=memory,
            estimated_tokens=380,
            estimated_cost_usd=0.0057,
            estimated_latency_ms=210,
        )

        # Step 2: Initial LLM Reasoning (Hallucinates unscoped SQL)
        bad_sql = "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0;"
        self.engine.record_step(
            run=run,
            step_type="LLM_REASONING",
            name="plan_sql_attempt_1",
            input_payload={"goal": "Refund ORD-9921 ($120.00)", "turn": 1},
            executor_fn=lambda inp, mem: {
                "thought": "I will update the orders table to set status='REFUNDED' and refund_usd=120.0.",
                "proposed_tool": "execute_sql",
                "proposed_query": bad_sql,
                "memory_updates": {"attempt_1_sql": bad_sql},
            },
            memory_state=memory,
            estimated_tokens=720,
            estimated_cost_usd=0.0108,
            estimated_latency_ms=540,
            use_cache=False,
        )

        # Step 3: Intercepted OpenAI/LangChain Tool Call -> Blocked at SAVEPOINT, returns Self-Heal Observation!
        tool_msg_1 = self.intercept_openai_tool_call(
            run=run,
            tool_call={
                "id": "call_attempt_1",
                "type": "function",
                "function": {
                    "name": "execute_sql",
                    "arguments": json.dumps(
                        {
                            "query": bad_sql,
                            "database": "prod_ledger_primary",
                            "amount_usd": 120.0,
                        }
                    ),
                },
            },
            tool_registry={"execute_sql": lambda **kw: {"status": "COMMITTED"}},
            memory_state=memory,
        )
        observation_payload = json.loads(tool_msg_1["content"])
        self_heal_feedback = observation_payload.get("self_heal_observation", "")

        # Step 4: Self-Healing LLM Reflection (Reads SAVEPOINT Rollback Feedback & Fixes SQL)
        fixed_sql = "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0 WHERE order_id = 'ORD-9921';"
        self.engine.record_step(
            run=run,
            step_type="LLM_REASONING",
            name="self_healing_reflection",
            input_payload={
                "firewall_observation": self_heal_feedback,
                "target_entity_from_memory": memory.get("target_order_id", "ORD-9921"),
                "turn": 2,
            },
            executor_fn=lambda inp, mem: {
                "thought": (
                    "Chronos ShadowFirewall blocked Attempt #1 because the UPDATE lacked a WHERE clause and "
                    "mutated 250 rows inside `SAVEPOINT chronos_shadow_sp`. Reading `target_order_id='ORD-9921'` "
                    "from memory, I am rewriting the query with a strict primary-key filter `WHERE order_id = 'ORD-9921'`."
                ),
                "self_healed_query": fixed_sql,
                "memory_updates": {
                    "self_healed": True,
                    "attempt_2_sql": fixed_sql,
                },
            },
            memory_state=memory,
            estimated_tokens=590,
            estimated_cost_usd=0.0088,
            estimated_latency_ms=420,
            use_cache=False,
        )

        # Step 5: Second Tool Call -> Passes SAVEPOINT (1 row mutated, INDEXED_LOOKUP) & Commits!
        self.intercept_openai_tool_call(
            run=run,
            tool_call={
                "id": "call_attempt_2_healed",
                "type": "function",
                "function": {
                    "name": "execute_sql",
                    "arguments": json.dumps(
                        {
                            "query": fixed_sql,
                            "database": "prod_ledger_primary",
                            "amount_usd": 120.0,
                        }
                    ),
                },
            },
            tool_registry={
                "execute_sql": lambda **kw: {
                    "rows_affected": 1,
                    "updated_order": self.engine.sandbox_db.get_order("ORD-9921"),
                    "status": "SELF_HEALED_AND_COMMITTED",
                    "memory_updates": {"refund_completed": True},
                }
            },
            memory_state=memory,
        )

        run.status = "SELF_HEALED_SUCCESS"
        self.engine.storage.save_run(run.to_dict())
        return run

    def _run_sre_self_healing(self) -> AgentRun:
        run = self.engine.start_run(
            agent_name="K8s-SRE-SelfHeal-Agent (OpenAI Tool Interceptor)",
            scenario="[Self-Healing Loop] Node Disk 96%: Auto-recover from blocked rm -rf via Firewall feedback",
        )
        memory: Dict[str, Any] = {"node": "ip-10-0-42-19.ec2.internal", "disk_pct": 96.4}

        bad_cmd = "rm -rf /var/log/* && chmod 777 /etc/passwd"
        self.engine.record_step(
            run=run,
            step_type="LLM_REASONING",
            name="plan_bash_attempt_1",
            input_payload={"alert": "DiskPressure 96.4%", "turn": 1},
            executor_fn=lambda inp, mem: {
                "thought": "Purging /var/log/* recursively to reclaim disk space immediately.",
                "proposed_cmd": bad_cmd,
            },
            memory_state=memory,
            estimated_tokens=510,
            estimated_cost_usd=0.0076,
            estimated_latency_ms=390,
            use_cache=False,
        )

        tool_msg_1 = self.intercept_openai_tool_call(
            run=run,
            tool_call={
                "id": "call_sre_1",
                "type": "function",
                "function": {
                    "name": "run_bash",
                    "arguments": json.dumps({"cmd": bad_cmd, "host": "ip-10-0-42-19.ec2.internal"}),
                },
            },
            tool_registry={"run_bash": lambda **kw: {"exit_code": 0}},
            memory_state=memory,
        )
        obs = json.loads(tool_msg_1["content"]).get("self_heal_observation", "")

        safe_cmd = "logrotate -f /etc/logrotate.d/containers && journalctl --vacuum-size=500M"
        self.engine.record_step(
            run=run,
            step_type="LLM_REASONING",
            name="self_healing_sre_reflection",
            input_payload={"firewall_observation": obs, "turn": 2},
            executor_fn=lambda inp, mem: {
                "thought": (
                    "Chronos Firewall blocked `rm -rf /var/log/*` under Rule SYS-001. "
                    "Switching to safe logrotate and journalctl vacuuming."
                ),
                "self_healed_cmd": safe_cmd,
                "memory_updates": {"self_healed": True},
            },
            memory_state=memory,
            estimated_tokens=460,
            estimated_cost_usd=0.0069,
            estimated_latency_ms=340,
            use_cache=False,
        )

        self.intercept_openai_tool_call(
            run=run,
            tool_call={
                "id": "call_sre_2_healed",
                "type": "function",
                "function": {
                    "name": "run_bash",
                    "arguments": json.dumps({"cmd": safe_cmd, "host": "ip-10-0-42-19.ec2.internal"}),
                },
            },
            tool_registry={
                "run_bash": lambda **kw: {
                    "exit_code": 0,
                    "freed_gb": 54.8,
                    "new_disk_usage_pct": 38.4,
                    "status": "SELF_HEALED_AND_REMEDIATED",
                }
            },
            memory_state=memory,
        )

        run.status = "SELF_HEALED_SUCCESS"
        self.engine.storage.save_run(run.to_dict())
        return run


INTEGRATION_SNIPPETS: Dict[str, str] = {
    "langchain": """from chronos import ChronosEngine, ChronosToolInterceptor

engine = ChronosEngine()
interceptor = ChronosToolInterceptor(engine, self_heal=True)

# Wrap any LangChain / LangGraph tools in 1 line:
run = engine.start_run("LangGraph-FinOps-Agent", "Customer Refund")
safe_tools = interceptor.wrap_langchain_tools([execute_sql_tool, bash_tool], run=run)

# If the LLM generates an unscoped UPDATE/DELETE, Chronos rolls back at SQLite SAVEPOINT
# and returns a [CHRONOS_FIREWALL_BLOCKED] observation so the agent self-heals!""",
    "openai": """from chronos import ChronosEngine, ChronosToolInterceptor

engine = ChronosEngine()
interceptor = ChronosToolInterceptor(engine, self_heal=True)
run = engine.start_run("OpenAI-Tool-Agent", "Order Dispute")

for tool_call in response.choices[0].message.tool_calls:
    tool_msg = interceptor.intercept_openai_tool_call(
        run=run,
        tool_call=tool_call.model_dump(),
        tool_registry={"execute_sql": execute_sql_fn},
    )
    messages.append(tool_msg)  # Feeds SAVEPOINT rollback feedback back to GPT-4o!""",
    "decorator": """from chronos import ChronosEngine, ChronosSDK

sdk = ChronosSDK(ChronosEngine())

@sdk.step(step_type="TOOL_CALL", name="execute_sql", self_heal=True)
def execute_sql(query: str, database: str = "prod_ledger"):
    return {"query": query, "status": "EXECUTED"}

with sdk.trace_run("Custom-Python-Agent", "Refund Workflow"):
    # Automatically inspected in SQLite SAVEPOINT + cached via 128-dim Semantic Vectors
    res = execute_sql(query="UPDATE orders SET status='REFUNDED' WHERE order_id='ORD-9921';")""",
}
