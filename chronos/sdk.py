"""Synchronous tools with explicit tracing and context-local run state."""
from contextvars import ContextVar
from functools import wraps
from .arguments import bind_tool_call
from .engine import ChronosEngine


class ChronosSDK:
    def __init__(self, engine=None):
        self.engine = engine or ChronosEngine()
        self._context = ContextVar(f"chronos-sdk-{id(self)}", default=None)

    def trace_run(self, agent_name: str, scenario: str):
        sdk = self

        class RunContext:
            def __enter__(self):
                self.run = sdk.engine.start_run(agent_name, scenario)
                self.token = sdk._context.set((self.run, {}))
                return self.run

            def __exit__(self, exc_type, exc_val, exc_tb):
                try:
                    with sdk.engine._lock:
                        if self.run.status == "RUNNING":
                            if exc_type:
                                self.run.status = "FAILED"
                            elif self.run.steps and self.run.steps[-1].status == "BLOCKED_BY_FIREWALL":
                                self.run.status = "HALTED_BY_FIREWALL"
                            else:
                                self.run.status = "COMPLETED"
                        sdk.engine.storage.save_run(self.run.to_dict())
                finally:
                    sdk._context.reset(self.token)

        return RunContext()

    def step(self, step_type="TOOL_CALL", name=None, tokens=0, cost_usd=0.0,
             latency_ms=0, self_heal=False, local_sql=False, use_cache=False,
             cache_version="1"):
        """SQL requires local_sql=True and is executed only by the local ledger.

        SQL callbacks are not invoked. Other callbacks execute after input policy.
        Cache is opt-in for pure reasoning only.
        """
        def decorator(fn):
            step_name = name or fn.__name__
            self.engine.firewall.allowed_tools.add(step_name)

            @wraps(fn)
            def wrapper(*args, **kwargs):
                context = self._context.get()
                if context is None:
                    raise RuntimeError("Guarded tools require an active trace_run context")
                payload, bound = bind_tool_call(fn, args, kwargs)
                run, memory = context

                def execute(inp, mem):
                    result = fn(*bound.args, **bound.kwargs)
                    return result if isinstance(result, dict) else {"result": result}

                return self.engine.record_step(
                    run, step_type, step_name, payload, execute, memory,
                    estimated_tokens=tokens, estimated_cost_usd=cost_usd,
                    estimated_latency_ms=latency_ms, halt_on_block=not self_heal,
                    local_sql=local_sql, use_cache=use_cache, cache_version=cache_version,
                ).output_payload
            return wrapper
        return decorator
