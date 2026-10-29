"""
ChronosEngine — Deterministic Time-Travel DAG & Snapshot Engine for Autonomous Agents.
Backed by SQLite WAL persistence (`ChronosStorage`) and Real SQLite SAVEPOINT Shadow Execution (`ShadowFirewall`).
"""

import copy
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Callable

from .firewall import ShadowFirewall, PolicyResult
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
            "total_tokens_used": self.total_tokens_used,
            "total_tokens_saved": self.total_tokens_saved,
            "total_cost_usd": self.total_cost_usd,
            "total_cost_saved_usd": self.total_cost_saved_usd,
            "total_latency_ms": self.total_latency_ms,
            "steps": [s.to_dict() for s in self.steps],
        }


class ChronosEngine:
    """
    Core runtime managing agent DAG execution traces, deterministic time-travel forks,
    ShadowFirewall SQLite SAVEPOINT inspection, SubgraphCache lookups, and SQLite persistence.
    """

    def __init__(self, db_path: Optional[str] = None, ledger_db_path: Optional[str] = None):
        self.runs: Dict[str, AgentRun] = {}
        self.sandbox_db = ShadowDatabaseSandbox(db_path=ledger_db_path)
        self.firewall = ShadowFirewall(max_auto_transfer_usd=500.0, sandbox_db=self.sandbox_db)
        self.cache = SubgraphCache()
        self.storage = ChronosStorage(db_path=db_path)
        self.fork_count = 0

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
            parent_run_id=parent_run_id,
            forked_from_step=forked_from_step,
        )
        self.runs[run_id] = run
        self.storage.save_run(run.to_dict())
        return run

    def record_step(
        self,
        run: AgentRun,
        step_type: str,
        name: str,
        input_payload: Dict[str, Any],
        executor_fn: Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]],
        memory_state: Dict[str, Any],
        estimated_tokens: int = 850,
        estimated_cost_usd: float = 0.0125,
        estimated_latency_ms: int = 620,
        use_cache: bool = True,
    ) -> ExecutionStep:
        step_index = len(run.steps) + 1
        step_id = f"step-{step_index}-{uuid.uuid4().hex[:6]}"
        state_hash = SubgraphCache.compute_hash(step_type, name, input_payload)

        # 1. If TOOL_CALL, inspect with ShadowFirewall (runs inside real SQLite SAVEPOINT for SQL tools!)
        policy_dict = None
        if step_type == "TOOL_CALL":
            policy_res: PolicyResult = self.firewall.inspect_tool_call(
                name,
                input_payload,
                commit_if_safe=True,
            )
            policy_dict = policy_res.to_dict()
            if not policy_res.allowed:
                step = ExecutionStep(
                    step_index=step_index,
                    step_id=step_id,
                    step_type=step_type,
                    name=name,
                    input_payload=copy.deepcopy(input_payload),
                    output_payload={
                        "error": "ShadowFirewall Blocked Execution (Rolled Back at SQLite SAVEPOINT)",
                        "action": policy_res.action,
                        "transaction_action": policy_res.shadow_diff.get("transaction_action", "ROLLED_BACK"),
                        "violations": [v.description for v in policy_res.violations],
                    },
                    memory_snapshot=copy.deepcopy(memory_state),
                    status="BLOCKED_BY_FIREWALL",
                    tokens_used=120,
                    tokens_saved=0,
                    cost_usd=0.0018,
                    cost_saved_usd=0.0,
                    latency_ms=4,
                    state_hash=state_hash,
                    policy_result=policy_dict,
                )
                run.steps.append(step)
                run.status = "HALTED_BY_FIREWALL"
                self.storage.save_run(run.to_dict())
                return step

        # 2. Check Semantic Subgraph Cache
        if use_cache:
            cached = self.cache.lookup(step_type, name, input_payload)
            if cached:
                output = copy.deepcopy(cached.output_payload)
                memory_state.update(output.get("memory_updates", {}))
                step = ExecutionStep(
                    step_index=step_index,
                    step_id=step_id,
                    step_type=step_type,
                    name=name,
                    input_payload=copy.deepcopy(input_payload),
                    output_payload=output,
                    memory_snapshot=copy.deepcopy(memory_state),
                    status="CACHED_HIT",
                    tokens_used=0,
                    tokens_saved=cached.tokens_saved_per_hit,
                    cost_usd=0.0,
                    cost_saved_usd=cached.cost_saved_per_hit_usd,
                    latency_ms=2,
                    state_hash=state_hash,
                    policy_result=policy_dict,
                )
                run.steps.append(step)
                self.storage.save_run(run.to_dict())
                return step

        # 3. Execute live step
        output = executor_fn(input_payload, memory_state)
        if policy_dict and "shadow_diff" in policy_dict:
            output["sqlite_transaction"] = policy_dict["shadow_diff"].get("transaction_action", "COMMITTED")
            output["query_plan"] = policy_dict["shadow_diff"].get("query_plan", [])

        memory_state.update(output.get("memory_updates", {}))

        if use_cache:
            self.cache.put(
                step_type=step_type,
                name=name,
                input_payload=input_payload,
                output_payload=output,
                tokens=estimated_tokens,
                cost_usd=estimated_cost_usd,
                latency_ms=estimated_latency_ms,
            )

        step = ExecutionStep(
            step_index=step_index,
            step_id=step_id,
            step_type=step_type,
            name=name,
            input_payload=copy.deepcopy(input_payload),
            output_payload=output,
            memory_snapshot=copy.deepcopy(memory_state),
            status="COMPLETED",
            tokens_used=estimated_tokens,
            tokens_saved=0,
            cost_usd=estimated_cost_usd,
            cost_saved_usd=0.0,
            latency_ms=estimated_latency_ms,
            state_hash=state_hash,
            policy_result=policy_dict,
        )
        run.steps.append(step)
        self.storage.save_run(run.to_dict())
        return step

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

        # Replay steps 1 .. target_step_index - 1 from immutable snapshot (Zero tokens spent!)
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
                latency_ms=1,
                state_hash=prev_step.state_hash,
                policy_result=copy.deepcopy(prev_step.policy_result),
            )
            forked_run.steps.append(replayed_step)
            restored_memory = copy.deepcopy(prev_step.memory_snapshot)

        # Execute the patched target step and any subsequent continuation steps
        if continuation_fn:
            continuation_fn(self, forked_run, patched_input, restored_memory)

        if forked_run.status == "RUNNING":
            forked_run.status = "FORKED_SUCCESS"

        self.storage.save_run(forked_run.to_dict())
        return forked_run

    def get_global_metrics(self) -> Dict[str, Any]:
        runs_list = list(self.runs.values())
        total_tokens_used = sum(r.total_tokens_used for r in runs_list)
        total_tokens_saved = sum(r.total_tokens_saved for r in runs_list)
        total_cost_usd = round(sum(r.total_cost_usd for r in runs_list), 4)
        total_cost_saved_usd = round(sum(r.total_cost_saved_usd for r in runs_list), 4)

        return {
            "total_runs": len(runs_list),
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
