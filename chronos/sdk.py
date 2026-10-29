"""
ChronosSDK — Drop-in Python Decorator SDK for LangChain, OpenAI, Anthropic & Custom Agents.
Usage:
    sdk = ChronosSDK(engine)

    @sdk.tool(name="execute_sql")
    def execute_sql(query: str):
        ...
"""

from functools import wraps
from typing import Dict, Any, Callable, Optional
from .engine import ChronosEngine, AgentRun


class ChronosSDK:
    """
    Developer-facing ergonomic SDK that wraps agent functions and tool calls
    into Chronos deterministic DAG traces automatically.
    """

    def __init__(self, engine: Optional[ChronosEngine] = None):
        self.engine = engine or ChronosEngine()
        self._active_run: Optional[AgentRun] = None
        self._active_memory: Dict[str, Any] = {}

    def trace_run(self, agent_name: str, scenario: str):
        """Context manager or decorator to start a deterministic agent trace."""
        sdk = self

        class _RunContext:
            def __enter__(self) -> AgentRun:
                sdk._active_memory = {}
                sdk._active_run = sdk.engine.start_run(agent_name=agent_name, scenario=scenario)
                return sdk._active_run

            def __exit__(self, exc_type, exc_val, exc_tb):
                if sdk._active_run and sdk._active_run.status == "RUNNING":
                    sdk._active_run.status = "COMPLETED" if exc_type is None else "FAILED"
                sdk._active_run = None

        return _RunContext()

    def step(
        self,
        step_type: str = "TOOL_CALL",
        name: Optional[str] = None,
        tokens: int = 750,
        cost_usd: float = 0.011,
        latency_ms: int = 480,
    ):
        """Decorator that intercepts an agent step/tool call with Chronos Firewall + Cache + Snapshot."""

        def decorator(fn: Callable):
            step_name = name or fn.__name__

            @wraps(fn)
            def wrapper(*args, **kwargs):
                if not self._active_run:
                    return fn(*args, **kwargs)

                input_payload = kwargs if kwargs else {"args": list(args)}

                def _exec(payload: Dict[str, Any], mem: Dict[str, Any]) -> Dict[str, Any]:
                    result = fn(**payload) if kwargs else fn(*args)
                    return result if isinstance(result, dict) else {"result": result}

                recorded = self.engine.record_step(
                    run=self._active_run,
                    step_type=step_type,
                    name=step_name,
                    input_payload=input_payload,
                    executor_fn=_exec,
                    memory_state=self._active_memory,
                    estimated_tokens=tokens,
                    estimated_cost_usd=cost_usd,
                    estimated_latency_ms=latency_ms,
                )
                return recorded.output_payload

            return wrapper

        return decorator
