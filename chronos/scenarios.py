"""
Realistic Production Scenarios for ChronosAI Demonstration.
Includes:
1. FinOps Autonomous Agent (hallucinates unbounded SQL UPDATE -> intercepted by ShadowFirewall -> fixed via Time-Travel Fork)
2. SecOps / SRE Agent (attempts unsafe shell cleanup -> intercepted -> fixed via Time-Travel Fork)
"""

from typing import Dict, Any, Optional
from .engine import ChronosEngine, AgentRun


def run_finops_incident_scenario(engine: ChronosEngine, use_cache: bool = True) -> AgentRun:
    """
    Simulates a 5-step FinOps autonomous agent investigating Ticket #TKT-8841 (Order ORD-9921).
    At Step 5, the agent attempts an unbounded SQL UPDATE without a WHERE clause, which is blocked
    by Chronos ShadowFirewall after simulating a $45.6M blast radius.
    """
    if not engine.demo_mode:
        raise PermissionError("Demonstrations require explicit demo mode")
    run = engine.start_run(
        agent_name="FinOps-Autonomous-v4",
        scenario="Ticket #TKT-8841: Duplicate Charge Refund & Ledger Sync (Order ORD-9921)",
    )
    memory: Dict[str, Any] = {}

    # Step 1: Load customer & ticket context
    engine.record_step(
        run=run,
        step_type="MEMORY_READ",
        name="fetch_customer_context",
        input_payload={"ticket_id": "TKT-8841", "customer_id": "CUST-44910"},
        executor_fn=lambda inp, mem: {
            "customer_tier": "ENTERPRISE_VIP",
            "order_id": "ORD-9921",
            "disputed_amount_usd": 120.0,
            "memory_updates": {"order_id": "ORD-9921", "disputed_amount_usd": 120.0, "customer_tier": "ENTERPRISE_VIP"},
        },
        memory_state=memory,
        estimated_tokens=640,
        estimated_cost_usd=0.0096,
        estimated_latency_ms=310,
        use_cache=use_cache,
    )

    # Step 2: LLM Reasoning - Diagnose ledger mismatch
    engine.record_step(
        run=run,
        step_type="LLM_REASONING",
        name="diagnose_billing_anomaly",
        input_payload={
            "model": "gpt-4o-production",
            "system_prompt": "You are an autonomous FinOps reconciliation agent.",
            "context_order": "ORD-9921",
        },
        executor_fn=lambda inp, mem: {
            "thought": "Customer CUST-44910 was billed twice on webhook retry. Need to verify Stripe charge ch_3Pq9921.",
            "next_action": "verify_stripe_charge",
            "memory_updates": {"stripe_charge_id": "ch_3Pq9921"},
        },
        memory_state=memory,
        estimated_tokens=1850,
        estimated_cost_usd=0.0278,
        estimated_latency_ms=1120,
        use_cache=use_cache,
    )

    # Step 3: Tool Call - Verify Stripe Charge
    engine.record_step(
        run=run,
        step_type="TOOL_CALL",
        name="verify_stripe_charge",
        input_payload={"charge_id": "ch_3Pq9921", "order_id": "ORD-9921"},
        executor_fn=lambda inp, mem: {
            "stripe_status": "CAPTURED_DUPLICATE",
            "captured_usd": 120.0,
            "refundable": True,
            "memory_updates": {"duplicate_verified": True},
        },
        memory_state=memory,
        estimated_tokens=920,
        estimated_cost_usd=0.0138,
        estimated_latency_ms=540,
        use_cache=use_cache,
    )

    # Step 4: LLM Reasoning - Hallucinates SQL without WHERE clause!
    engine.record_step(
        run=run,
        step_type="LLM_REASONING",
        name="synthesize_sql_remediation",
        input_payload={
            "model": "gpt-4o-production",
            "prompt": "Generate SQL to mark the disputed order as REFUNDED with refund_usd = 120.0",
            "temperature": 0.7,
        },
        executor_fn=lambda inp, mem: {
            "thought": "I will update the orders table to set status='REFUNDED' and refund_usd=120.0.",
            "generated_sql": "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0;",
            "warning_note": "LLM omitted WHERE order_id = 'ORD-9921' due to context drift.",
            "memory_updates": {"candidate_sql": "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0;"},
        },
        memory_state=memory,
        estimated_tokens=1640,
        estimated_cost_usd=0.0246,
        estimated_latency_ms=980,
        use_cache=False,
    )

    # Step 5: Tool Call - Execute SQL (Blocked by ShadowFirewall!)
    engine.record_step(
        run=run,
        step_type="TOOL_CALL",
        name="execute_sql",
        input_payload={
            "query": memory.get("candidate_sql", "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0;"),
            "database": "ledger",
        },
        executor_fn=lambda inp, mem: {"rows_affected": 912400},
        memory_state=memory,
        estimated_tokens=450,
        estimated_cost_usd=0.0068,
        estimated_latency_ms=420,
        use_cache=False,
    )

    return run


def continue_forked_finops_run(
    engine: ChronosEngine,
    forked_run: AgentRun,
    patched_input: Dict[str, Any],
    restored_memory: Dict[str, Any],
) -> None:
    """
    Continuation callback when a developer time-travels to Step 4 or Step 5
    and patches the SQL query or prompt to scope it safely (e.g. adding WHERE order_id = 'ORD-9921').
    """
    target_step = forked_run.forked_from_step or 5

    # If forked from Step 4 (LLM synthesis step), record the patched Step 4 first
    if target_step <= 4:
        safe_sql = patched_input.get(
            "generated_sql",
            "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0 WHERE order_id = 'ORD-9921';",
        )
        engine.record_step(
            run=forked_run,
            step_type="LLM_REASONING",
            name="synthesize_sql_remediation",
            input_payload={
                "model": "gpt-4o-production",
                "prompt": patched_input.get(
                    "prompt",
                    "Generate scoped SQL with explicit WHERE order_id = 'ORD-9921' to mark order as REFUNDED.",
                ),
                "temperature": 0.0,
            },
            executor_fn=lambda inp, mem: {
                "thought": "Applying strict primary-key constraint WHERE order_id = 'ORD-9921'.",
                "generated_sql": safe_sql,
                "memory_updates": {"candidate_sql": safe_sql},
            },
            memory_state=restored_memory,
            estimated_tokens=510,
            estimated_cost_usd=0.0076,
            estimated_latency_ms=340,
            use_cache=False,
        )
        sql_to_run = safe_sql
    else:
        sql_to_run = patched_input.get(
            "query",
            "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0 WHERE order_id = 'ORD-9921';",
        )

    # Step 5: Execute Patched SQL (Inspected by ShadowFirewall)
    step5 = engine.record_step(
        run=forked_run,
        step_type="TOOL_CALL",
        name="execute_sql",
        input_payload={
            "query": sql_to_run,
            "database": patched_input.get("database", "ledger"),
        },
        executor_fn=lambda inp, mem: {
            "status": "COMMITTED_SAFELY",
            "rows_affected": 1,
            "order_id": "ORD-9921",
            "memory_updates": {"ledger_synced": True},
        },
        memory_state=restored_memory,
        estimated_tokens=380,
        estimated_cost_usd=0.0057,
        estimated_latency_ms=190,
        use_cache=False,
    )

    # If Step 5 passed the ShadowFirewall, run Step 6 (Stripe Refund Payout) to complete workflow!
    if step5.status != "BLOCKED_BY_FIREWALL":
        amount = float(patched_input.get("amount_usd", restored_memory.get("disputed_amount_usd", 120.0)))
        engine.record_step(
            run=forked_run,
            step_type="TOOL_CALL",
            name="issue_refund",
            input_payload={
                "charge_id": restored_memory.get("stripe_charge_id", "ch_3Pq9921"),
                "order_id": "ORD-9921",
                "amount_usd": amount,
            },
            executor_fn=lambda inp, mem: {
                "refund_id": "re_9921_chronos_verified",
                "amount_refunded_usd": amount,
                "status": "SIMULATED",
                "executed": False,
                "memory_updates": {"workflow_completed": True},
            },
            memory_state=restored_memory,
            estimated_tokens=310,
            estimated_cost_usd=0.0046,
            estimated_latency_ms=210,
            use_cache=True,
        )


def run_sre_secops_scenario(engine: ChronosEngine) -> AgentRun:
    """
    Simulates a Kubernetes SRE Auto-Remediation Agent that diagnoses disk pressure
    and attempts a destructive `rm -rf` command, blocked by ShadowFirewall rule SYS-001.
    """
    if not engine.demo_mode:
        raise PermissionError("Demonstrations require explicit demo mode")
    run = engine.start_run(
        agent_name="K8s-SecOps-AutoHealer",
        scenario="Alert #ALT-309: Node DiskPressure 94% on prod-us-east-cluster",
    )
    memory: Dict[str, Any] = {}

    engine.record_step(
        run=run,
        step_type="MEMORY_READ",
        name="fetch_prometheus_metrics",
        input_payload={"cluster": "prod-us-east", "node": "ip-10-0-42-19.ec2.internal"},
        executor_fn=lambda inp, mem: {
            "disk_usage_pct": 94.2,
            "top_directory": "/var/log/containers (68.4 GB)",
            "memory_updates": {"target_node": "ip-10-0-42-19.ec2.internal"},
        },
        memory_state=memory,
        estimated_tokens=720,
        estimated_cost_usd=0.0108,
        estimated_latency_ms=290,
    )

    engine.record_step(
        run=run,
        step_type="LLM_REASONING",
        name="plan_disk_cleanup",
        input_payload={"model": "claude-3-7-sonnet", "node": "ip-10-0-42-19.ec2.internal"},
        executor_fn=lambda inp, mem: {
            "thought": "Disk is full. I will purge all logs and reset permissions.",
            "proposed_cmd": "rm -rf /var/log/* && chmod 777 /etc/kubernetes",
            "memory_updates": {"proposed_cmd": "rm -rf /var/log/* && chmod 777 /etc/kubernetes"},
        },
        memory_state=memory,
        estimated_tokens=1490,
        estimated_cost_usd=0.0223,
        estimated_latency_ms=870,
        use_cache=False,
    )

    engine.record_step(
        run=run,
        step_type="TOOL_CALL",
        name="run_bash",
        input_payload={
            "cmd": "rm -rf /var/log/* && chmod 777 /etc/kubernetes",
            "host": "ip-10-0-42-19.ec2.internal",
        },
        executor_fn=lambda inp, mem: {"exit_code": 0},
        memory_state=memory,
        estimated_tokens=410,
        estimated_cost_usd=0.0061,
        estimated_latency_ms=180,
        use_cache=False,
    )

    return run
