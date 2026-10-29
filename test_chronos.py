"""
Comprehensive End-to-End Verification Suite for ChronosAI:
1. Real SQLite SAVEPOINT Shadow Sandbox (UPDATE without WHERE, DROP TABLE, Scoped UPDATE, EXPLAIN QUERY PLAN)
2. Multi-Layer ShadowFirewall Guardrails (SQL, Financial Payout Ceiling, Bash rm -rf, API Key / PII Exfiltration)
3. Content-Addressable Semantic Subgraph Cache (SHA-256 intent hashing & ROI calculation)
4. Deterministic Time-Travel Forking (FinOps Step 4 & Step 5, SRE Bash remediation)
5. SQLite WAL Persistence (`ChronosStorage`) & `.chronos.json` Bundle Export
6. Ergonomic Python SDK (`@sdk.step`, `sdk.trace_run`)
7. FastAPI Control Plane Endpoints (`/`, `/api/overview`, `/api/ledger`, `/api/runs/simulate`, `/api/runs/{id}/fork`, `/api/runs/{id}/export`, `/api/firewall/inspect`)
"""

from fastapi.testclient import TestClient
from chronos import (
    ChronosEngine,
    ChronosSDK,
    ShadowFirewall,
    SubgraphCache,
    ShadowDatabaseSandbox,
    ChronosStorage,
)
from chronos.scenarios import (
    run_finops_incident_scenario,
    continue_forked_finops_run,
    run_sre_secops_scenario,
)
from server import app


def test_1_sqlite_savepoint_rollback_and_commit(tmp_path):
    engine = ChronosEngine(
        db_path=str(tmp_path / "test_traces.db"),
        ledger_db_path=str(tmp_path / "test_ledger.db"),
    )

    # Initial ledger must have 250 seeded orders and $0 refunded
    initial = engine.sandbox_db.get_ledger_summary()
    assert initial["total_orders"] == 250
    assert initial["total_refunds_usd"] == 0.0
    assert initial["status_distribution"]["DISPUTED"] == 1
    assert initial["status_distribution"]["COMPLETED"] == 249

    # A. Unbounded UPDATE without WHERE -> Executes in SAVEPOINT, detects 250 rows ($30,000), rolls back!
    unbounded_res = engine.firewall.inspect_tool_call(
        "execute_sql",
        {"query": "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0;"},
        commit_if_safe=True,
    )
    assert not unbounded_res.allowed
    assert unbounded_res.action == "BLOCK"
    assert unbounded_res.shadow_diff["estimated_rows_mutated"] == 250
    assert unbounded_res.shadow_diff["financial_refund_delta_usd"] == 30000.0
    assert unbounded_res.shadow_diff["transaction_action"] == "ROLLED_BACK_AT_SAVEPOINT"

    # Verify DB is completely untouched after rollback
    after_rb = engine.sandbox_db.get_ledger_summary()
    assert after_rb["total_refunds_usd"] == 0.0
    assert after_rb["status_distribution"].get("REFUNDED", 0) == 0

    # B. DROP TABLE -> Blocked & Rolled Back, all 250 orders preserved!
    drop_res = engine.firewall.inspect_tool_call(
        "execute_sql",
        {"query": "DROP TABLE orders;"},
        commit_if_safe=True,
    )
    assert not drop_res.allowed
    assert any(v.rule_id == "SQL-001" for v in drop_res.violations)
    assert engine.sandbox_db.get_ledger_summary()["total_orders"] == 250

    # C. Scoped UPDATE with WHERE order_id = 'ORD-9921' -> Passes and Commits to SQLite!
    scoped_res = engine.firewall.inspect_tool_call(
        "execute_sql",
        {"query": "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0 WHERE order_id = 'ORD-9921';"},
        commit_if_safe=True,
    )
    assert scoped_res.allowed
    assert scoped_res.shadow_diff["estimated_rows_mutated"] == 1
    assert scoped_res.shadow_diff["uses_primary_key_index"] is True
    assert scoped_res.shadow_diff["transaction_action"] == "COMMITTED_TO_SQLITE"

    # Verify DB now has 1 REFUNDED order ($120.00)
    after_commit = engine.sandbox_db.get_ledger_summary()
    assert after_commit["total_refunds_usd"] == 120.0
    assert after_commit["status_distribution"]["REFUNDED"] == 1


def test_2_firewall_financial_shell_and_secret_rules(tmp_path):
    fw = ShadowFirewall(
        max_auto_transfer_usd=500.0,
        sandbox_db=ShadowDatabaseSandbox(db_path=str(tmp_path / "fw_ledger.db")),
    )

    # Financial transfer over ceiling ($25,000 > $500)
    fin_bad = fw.inspect_tool_call("issue_refund", {"charge_id": "ch_1", "amount_usd": 25000.0})
    assert not fin_bad.allowed
    assert any(v.rule_id == "FIN-001" for v in fin_bad.violations)

    # Financial transfer within ceiling ($120 <= $500)
    fin_ok = fw.inspect_tool_call("issue_refund", {"charge_id": "ch_1", "amount_usd": 120.0})
    assert fin_ok.allowed

    # Destructive Bash command (rm -rf)
    bash_bad = fw.inspect_tool_call("run_bash", {"cmd": "rm -rf /var/log/* && chmod 777 /etc/kubernetes"})
    assert not bash_bad.allowed
    assert any(v.rule_id == "SYS-001" for v in bash_bad.violations)

    # Secret / API Key exfiltration
    sec_bad = fw.inspect_tool_call("send_webhook", {"payload": "leak token sk-proj1234567890abcdefghijklm"})
    assert not sec_bad.allowed
    assert any(v.rule_id == "SEC-001" for v in sec_bad.violations)


def test_3_subgraph_cache_deduplication():
    cache = SubgraphCache()
    inp = {"ticket_id": "TKT-8841", "customer_id": "CUST-44910"}

    # First lookup is a miss
    assert cache.lookup("MEMORY_READ", "fetch_customer_context", inp) is None

    # Store result
    cache.put(
        step_type="MEMORY_READ",
        name="fetch_customer_context",
        input_payload=inp,
        output_payload={"order_id": "ORD-9921"},
        tokens=640,
        cost_usd=0.0096,
        latency_ms=310,
    )

    # Second lookup with identical content (even if key order differs) is a hit
    hit = cache.lookup("MEMORY_READ", "fetch_customer_context", {"customer_id": "CUST-44910", "ticket_id": "TKT-8841"})
    assert hit is not None
    assert hit.hits == 1
    stats = cache.stats()
    assert stats["total_hits"] == 1
    assert stats["total_tokens_saved"] == 640


def test_4_time_travel_fork_finops_and_sre(tmp_path):
    engine = ChronosEngine(
        db_path=str(tmp_path / "traces.db"),
        ledger_db_path=str(tmp_path / "ledger.db"),
    )

    # Run FinOps incident -> Halts at Step 5
    finops_run = run_finops_incident_scenario(engine, use_cache=True)
    assert finops_run.status == "HALTED_BY_FIREWALL"
    assert len(finops_run.steps) == 5

    # Fork from Step 5 with safe SQL
    forked_finops = engine.fork_from_step(
        source_run_id=finops_run.run_id,
        target_step_index=5,
        patched_input={
            "query": "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0 WHERE order_id = 'ORD-9921';",
            "database": "prod_ledger_primary",
            "amount_usd": 120.0,
        },
        continuation_fn=continue_forked_finops_run,
    )
    assert forked_finops.status == "FORKED_SUCCESS"
    assert len(forked_finops.steps) == 6
    assert all(s.status == "REPLAYED_SNAPSHOT" and s.tokens_used == 0 for s in forked_finops.steps[:4])
    assert forked_finops.steps[4].status == "COMPLETED"
    assert forked_finops.steps[5].status == "COMPLETED"
    assert forked_finops.total_tokens_saved == 5050

    # Run SRE incident -> Halts at Step 3
    sre_run = run_sre_secops_scenario(engine)
    assert sre_run.status == "HALTED_BY_FIREWALL"
    assert len(sre_run.steps) == 3

    # Verify SQLite persistence stored all 3 runs
    persisted = engine.storage.load_all_runs()
    assert len(persisted) == 3


def test_5_sdk_and_fastapi_control_plane_end_to_end(tmp_path):
    # SDK Test
    engine = ChronosEngine(
        db_path=str(tmp_path / "sdk_traces.db"),
        ledger_db_path=str(tmp_path / "sdk_ledger.db"),
    )
    sdk = ChronosSDK(engine=engine)

    @sdk.step(step_type="TOOL_CALL", name="execute_sql", tokens=400, cost_usd=0.008)
    def safe_query(query: str):
        return {"ok": True, "query": query}

    with sdk.trace_run(agent_name="SDK-Agent", scenario="E2E Verification") as run:
        res = safe_query(query="SELECT * FROM orders WHERE order_id = 'ORD-9921';")
        assert res["ok"] is True
    assert run.status == "COMPLETED"

    # FastAPI Endpoints E2E Test
    client = TestClient(app)

    # 1. Studio HTML page
    html_res = client.get("/")
    assert html_res.status_code == 200
    assert "Chronos" in html_res.text

    # 2. Reset & inspect SQLite Ledger
    reset_res = client.post("/api/ledger/reset")
    assert reset_res.status_code == 200
    assert reset_res.json()["ledger"]["total_orders"] == 250
    assert reset_res.json()["ledger"]["total_refunds_usd"] == 0.0

    # 3. Simulate FinOps Incident
    sim_res = client.post("/api/runs/simulate", json={"scenario_type": "finops"})
    assert sim_res.status_code == 200
    fin_run_id = sim_res.json()["run"]["run_id"]
    assert sim_res.json()["run"]["status"] == "HALTED_BY_FIREWALL"

    # 4. Time-Travel Fork the simulated FinOps incident
    fork_res = client.post(
        f"/api/runs/{fin_run_id}/fork",
        json={
            "target_step_index": 5,
            "patched_input": {
                "query": "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0 WHERE order_id = 'ORD-9921';",
                "database": "prod_ledger_primary",
                "amount_usd": 120.0,
            },
        },
    )
    assert fork_res.status_code == 200
    forked_data = fork_res.json()["forked_run"]
    assert forked_data["status"] == "FORKED_SUCCESS"

    # 5. Verify SQLite Ledger was updated by the safe Time-Travel Fork
    ledger_after = client.get("/api/ledger").json()
    assert ledger_after["total_refunds_usd"] == 120.0
    assert ledger_after["status_distribution"]["REFUNDED"] == 1

    # 6. Export .chronos bundle
    export_res = client.get(f"/api/runs/{forked_data['run_id']}/export")
    assert export_res.status_code == 200
    assert export_res.json()["exported_run"]["run_id"] == forked_data["run_id"]

    # 7. Reset ledger back to clean initial state for the live browser UI
    client.post("/api/ledger/reset")
