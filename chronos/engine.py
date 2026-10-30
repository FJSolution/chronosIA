"""
ChronosEngine — sequential execution traces, local SQL policies and explicit continuations.
Backed by SQLite WAL persistence (`ChronosStorage`) and Real SQLite SAVEPOINT Shadow Execution (`ShadowFirewall`).
"""

import os
from pathlib import Path
import copy
from threading import RLock
from .locking import synchronized
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Callable

from .firewall import ShadowFirewall, PolicyResult, PolicyViolation
from .cache import SubgraphCache
from .storage import ChronosStorage
from .sandbox_db import ShadowDatabaseSandbox


@dataclass
class ExecutionStep:
    step_index: int
    step_id: str
    step_type: str  # "LLM_REASONING", "TOOL_CALL", "MEMORY_READ", "POLICY_GATE"
    name: str
    input_payload: Dict[str, Any]
    output_payload: Dict[str, Any]
    memory_snapshot: Dict[str, Any]
    status: str  # "COMPLETED", "BLOCKED_BY_FIREWALL", "REPLAYED_SNAPSHOT", "CACHED_HIT"
    tokens_used: int = 0
    tokens_saved: int = 0
    cost_usd: float = 0.0
    cost_saved_usd: float = 0.0
    latency_ms: int = 0
    state_hash: str = ""
    policy_result: Optional[Dict[str, Any]] = None
    timestamp: float = field(default_factory=time.time)
    metrics_kind: str = "ESTIMATED"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentRun:
    run_id: str
    agent_name: str
    scenario: str
    status: str  # "COMPLETED", "HALTED_BY_FIREWALL", "FORKED_SUCCESS"
    parent_run_id: Optional[str] = None
    forked_from_step: Optional[int] = None
    steps: List[ExecutionStep] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    execution_mode: str = "LOCAL"

    @property
    def total_tokens_used(self) -> int:
        return sum(s.tokens_used for s in self.steps)

    @property
    def total_tokens_saved(self) -> int:
        return sum(s.tokens_saved for s in self.steps)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(s.cost_usd for s in self.steps), 4)

    @property
    def total_cost_saved_usd(self) -> float:
        return round(sum(s.cost_saved_usd for s in self.steps), 4)

    @property
    def total_latency_ms(self) -> int:
        return sum(s.latency_ms for s in self.steps)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "scenario": self.scenario,
            "status": self.status,
            "parent_run_id": self.parent_run_id,
            "forked_from_step": self.forked_from_step,
            "created_at": self.created_at,
            "execution_mode": self.execution_mode,
            "total_tokens_used": self.total_tokens_used,
            "total_tokens_saved": self.total_tokens_saved,
            "total_cost_usd": self.total_cost_usd,
            "total_cost_saved_usd": self.total_cost_saved_usd,
            "total_latency_ms": self.total_latency_ms,
            "steps": [s.to_dict() for s in self.steps],
        }


class ChronosEngine:
    """
    Core runtime managing sequential traces, explicit continuations,
    ShadowFirewall SQLite SAVEPOINT inspection, SubgraphCache lookups, and SQLite persistence.
    """

    def __init__(self, db_path: Optional[str] = None, ledger_db_path: Optional[str] = None, demo_mode: bool = False):
        self._lock = RLock()
        self.demo_mode = demo_mode
        self.runs: Dict[str, AgentRun] = {}
        self.sandbox_db = ShadowDatabaseSandbox(db_path=ledger_db_path, seed_demo=demo_mode)
        self.firewall = ShadowFirewall(max_auto_transfer_usd=500.0, sandbox_db=self.sandbox_db)
        if demo_mode:
            self.firewall.allowed_shell_commands = frozenset({
                "logrotate -f /etc/logrotate.d/containers && journalctl --vacuum-size=500M"
            })
        self.cache = SubgraphCache()
        self.storage = ChronosStorage(db_path=db_path)
        for saved in self.storage.load_all_runs():
            fields = {k: v for k, v in saved.items() if k in AgentRun.__dataclass_fields__ and k != "steps"}
            run = AgentRun(**fields)
            run.steps = [ExecutionStep(**{k: v for k, v in step.items() if k in ExecutionStep.__dataclass_fields__}) for step in saved.get("steps", [])]
            if run.status == "RUNNING":
                run.status = "INTERRUPTED"
                self.storage.save_run(run.to_dict())
            self.runs[run.run_id] = run
        self.fork_count = sum(r.parent_run_id is not None for r in self.runs.values())

    @classmethod
    def from_environment(cls):
        """Shared file configuration for the HTTP and stdio entry points."""
        demo = os.environ.get("CHRONOS_DEMO_MODE", "0") == "1"
        root = Path(os.environ.get("CHRONOS_DATA_DIR", Path(__file__).parent.parent))
        root.mkdir(parents=True, exist_ok=True)
        prefix = "chronos_demo" if demo else "chronos"
        return cls(db_path=os.environ.get("CHRONOS_TRACE_DB", str(root / f"{prefix}_traces.db")),
                   ledger_db_path=os.environ.get("CHRONOS_LEDGER_DB", str(root / f"{prefix}_ledger.db")),
                   demo_mode=demo)

    @synchronized
    def start_run(
        self,
        agent_name: str,
        scenario: str,
        parent_run_id: Optional[str] = None,
        forked_from_step: Optional[int] = None,
    ) -> AgentRun:
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        run = AgentRun(
            run_id=run_id,
            agent_name=agent_name,
            scenario=scenario,
            status="RUNNING",
            execution_mode="DEMO" if self.demo_mode else "LOCAL",
            parent_run_id=parent_run_id,
            forked_from_step=forked_from_step,
        )
        self.runs[run_id] = run
        self.storage.save_run(run.to_dict())
        return run

    @synchronized
    def record_step(
        self, run: AgentRun, step_type: str, name: str, input_payload: Dict[str, Any],
        executor_fn: Callable, memory_state: Dict[str, Any], estimated_tokens: int = 0,
        estimated_cost_usd: float = 0.0, estimated_latency_ms: int = 0,
        use_cache: bool = False, halt_on_block: bool = True, local_sql: bool = True,
        cache_version: str = "1",
    ) -> ExecutionStep:
        if self.runs.get(run.run_id) is not run or run.status != "RUNNING":
            raise ValueError("Only an active RUNNING execution can accept new steps")
        started = time.perf_counter()
        input_payload = copy.deepcopy(input_payload)
        before_memory = copy.deepcopy(memory_state)
        # Tools and mutable data reads are never cached. Pure reasoning is opt-in.
        use_cache = use_cache and step_type == "LLM_REASONING"
        cache_input = {"input": input_payload, "memory": before_memory, "version": cache_version}
        policy_dict = None
        status = "COMPLETED"
        tokens_saved = 0
        cost_saved = 0.0
        error = None
        output = {}
        try:
            if step_type == "TOOL_CALL":
                self.cache.clear()
                is_sql = self.firewall.is_sql(name, input_payload)
                if is_sql and not local_sql:
                    policy = PolicyResult(False, 99, "BLOCK", [PolicyViolation(
                        "SQL-BACKEND", "CRITICAL", "External SQL callbacks are not protected by this local ledger",
                        "Use the explicit local ledger execution mode; other databases need their own transactional adapter.")])
                else:
                    policy = self.firewall.inspect_tool_call(name, input_payload, commit_if_safe=is_sql)
                policy_dict = policy.to_dict()
                if not policy.allowed:
                    status = "BLOCKED_BY_FIREWALL"
                    if halt_on_block:
                        run.status = "HALTED_BY_FIREWALL"
                    output = {
                        "error": "Execution blocked before commit",
                        "action": policy.action,
                        "violations": [v.description for v in policy.violations],
                        "transaction_action": policy.shadow_diff.get("transaction_action", "NOT_EXECUTED"),
                        "self_heal_observation": "[CHRONOS_FIREWALL_BLOCKED] " + "; ".join(v.description for v in policy.violations),
                    }
                elif is_sql:
                    # The ledger is the only SQL executor. Never run a second callback.
                    output = {"status": policy.shadow_diff["transaction_action"],
                              "sqlite_transaction": policy.shadow_diff["transaction_action"],
                              "rows_affected": policy.shadow_diff["estimated_rows_mutated"],
                              "rows": policy.shadow_diff["result_rows"],
                              "query_plan": policy.shadow_diff["query_plan"]}
                else:
                    output = executor_fn(copy.deepcopy(input_payload), memory_state)
            else:
                cached = self.cache.lookup(step_type, name, cache_input) if use_cache else None
                if cached:
                    status = "CACHED_HIT"
                    output = copy.deepcopy(cached.output_payload)
                    output["_cache_match_mode"] = "EXACT_SHA256"
                    output["_cache_similarity"] = 1.0
                    tokens_saved = cached.tokens_saved_per_hit
                    cost_saved = cached.cost_saved_per_hit_usd
                else:
                    output = executor_fn(copy.deepcopy(input_payload), memory_state)
                    if use_cache:
                        self.cache.put(step_type, name, cache_input, output, estimated_tokens,
                                       estimated_cost_usd, int((time.perf_counter() - started) * 1000))
            if not isinstance(output, dict):
                raise TypeError("Step executor must return an object")
            if status != "BLOCKED_BY_FIREWALL":
                memory_state.update(output.get("memory_updates", {}))
        except Exception as exc:
            run.status = "FAILED"
            status = "FAILED"
            output = {"error": type(exc).__name__, "message": str(exc)}
            error = exc
        digest = SubgraphCache.compute_hash(step_type, name, {
            "input": input_payload, "output": output, "memory": memory_state, "status": status})
        step = ExecutionStep(
            step_index=len(run.steps) + 1, step_id=f"step-{uuid.uuid4().hex}",
            step_type=step_type, name=name, input_payload=input_payload,
            output_payload=copy.deepcopy(output), memory_snapshot=copy.deepcopy(memory_state),
            status=status, tokens_used=estimated_tokens if status == "COMPLETED" else 0,
            cost_usd=estimated_cost_usd if status == "COMPLETED" else 0,
            tokens_saved=tokens_saved, cost_saved_usd=cost_saved,
            latency_ms=int((time.perf_counter() - started) * 1000),
            state_hash=digest, policy_result=policy_dict,
            metrics_kind="ESTIMATED" if estimated_tokens or estimated_cost_usd else "NOT_REPORTED",
        )
        run.steps.append(step)
        self.storage.save_run(run.to_dict())
        if error is not None:
            raise error
        return step

    @synchronized
    def fork_from_step(
        self,
        source_run_id: str,
        target_step_index: int,
        patched_input: Dict[str, Any],
        continuation_fn: Optional[Callable[["ChronosEngine", AgentRun, Dict[str, Any], Dict[str, Any]], None]] = None,
    ) -> AgentRun:
        """
        Time-Travel Replay:
        Clones steps 1..(target_step_index - 1) deterministically at 0 token cost and 0ms LLM latency,
        restores exact memory snapshot, applies `patched_input` at `target_step_index`, and continues execution.
        """
        if not callable(continuation_fn):
            raise ValueError("Fork requires an explicit continuation; external state is not restored")
        if source_run_id not in self.runs:
            raise ValueError(f"Run {source_run_id} not found")

        source_run = self.runs[source_run_id]
        if target_step_index < 1 or target_step_index > len(source_run.steps):
            raise ValueError(f"Invalid step index {target_step_index}")

        self.fork_count += 1
        forked_run = self.start_run(
            agent_name=source_run.agent_name,
            scenario=f"[Time-Travel Fork @ Step #{target_step_index}] {source_run.scenario}",
            parent_run_id=source_run.run_id,
            forked_from_step=target_step_index,
        )

        # Replay steps 1 .. target_step_index - 1 from recorded memory without rerunning earlier callbacks
        restored_memory: Dict[str, Any] = {}
        for prev_step in source_run.steps[: target_step_index - 1]:
            saved_tokens = prev_step.tokens_used + prev_step.tokens_saved
            saved_cost = prev_step.cost_usd + prev_step.cost_saved_usd
            replayed_step = ExecutionStep(
                step_index=prev_step.step_index,
                step_id=f"replay-{prev_step.step_id}",
                step_type=prev_step.step_type,
                name=prev_step.name,
                input_payload=copy.deepcopy(prev_step.input_payload),
                output_payload=copy.deepcopy(prev_step.output_payload),
                memory_snapshot=copy.deepcopy(prev_step.memory_snapshot),
                status="REPLAYED_SNAPSHOT",
                tokens_used=0,
                tokens_saved=saved_tokens,
                cost_usd=0.0,
                cost_saved_usd=saved_cost,
                latency_ms=0,
                state_hash=prev_step.state_hash,
                policy_result=copy.deepcopy(prev_step.policy_result),
            )
            forked_run.steps.append(replayed_step)
            restored_memory = copy.deepcopy(prev_step.memory_snapshot)

        # Execute the patched target step and any subsequent continuation steps
        try:
            continuation_fn(self, forked_run, patched_input, restored_memory)
            if len(forked_run.steps) < target_step_index:
                raise ValueError("Continuation did not execute the target step")
        except Exception:
            forked_run.status = "FAILED"
            self.storage.save_run(forked_run.to_dict())
            raise

        if forked_run.status == "RUNNING":
            forked_run.status = "HALTED_BY_FIREWALL" if forked_run.steps[-1].status == "BLOCKED_BY_FIREWALL" else "FORKED_SUCCESS"

        self.storage.save_run(forked_run.to_dict())
        return forked_run

    @synchronized
    def get_global_metrics(self) -> Dict[str, Any]:
        runs_list = list(self.runs.values())
        total_tokens_used = sum(r.total_tokens_used for r in runs_list)
        total_tokens_saved = sum(r.total_tokens_saved for r in runs_list)
        total_cost_usd = round(sum(r.total_cost_usd for r in runs_list), 4)
        total_cost_saved_usd = round(sum(r.total_cost_saved_usd for r in runs_list), 4)

        return {
            "total_runs": len(runs_list),
            "metrics_kind": "ESTIMATED",
            "execution_mode": "DEMO" if self.demo_mode else "LOCAL",
            "time_travel_forks": self.fork_count,
            "firewall_inspected": self.firewall.total_inspected,
            "firewall_blocked": self.firewall.total_blocked,
            "total_tokens_used": total_tokens_used,
            "total_tokens_saved": total_tokens_saved,
            "total_cost_usd": total_cost_usd,
            "total_cost_saved_usd": total_cost_saved_usd,
            "savings_ratio_pct": round(
                (total_tokens_saved / (total_tokens_used + total_tokens_saved) * 100.0)
                if (total_tokens_used + total_tokens_saved) > 0
                else 0.0,
                1,
            ),
            "cache": self.cache.stats(),
            "ledger_summary": self.sandbox_db.get_ledger_summary(limit=8),
        }
