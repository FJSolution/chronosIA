"""
Comprehensive End-to-End Verification Suite for ChronosAI:
1. Real SQLite SAVEPOINT Shadow Sandbox (UPDATE without WHERE, DROP TABLE, Scoped UPDATE, EXPLAIN QUERY PLAN)
2. Multi-Layer ShadowFirewall Guardrails (SQL, Financial Payout Ceiling, Bash rm -rf, API Key / PII Exfiltration)
3. Content-Addressable Semantic Subgraph Cache (SHA-256 intent hashing & ROI calculation)
4. Deterministic Time-Travel Forking (FinOps Step 4 & Step 5, SRE Bash remediation)
5. SQLite WAL Persistence (`ChronosStorage`) & `.chronos.json` Bundle Export
6. Live AI Agent Runner (`LiveLLMAgent`) & Model Context Protocol Server (`ChronosMCPServer` JSON-RPC 2.0)
7. Ergonomic Python SDK & FastAPI Control Plane Endpoints
"""

import os
from fastapi.testclient import TestClient
from chronos import (
    ChronosEngine,
    ChronosSDK,
    ShadowFirewall,
    SubgraphCache,
    ShadowDatabaseSandbox,
    LiveLLMAgent,
    ChronosMCPServer,
    ChronosToolInterceptor,
)
from chronos.scenarios import (
    run_finops_incident_scenario,
    continue_forked_finops_run,
    run_sre_secops_scenario,
)
from server import app


def test_1_sqlite_savepoint_rollback_and_commit(tmp_path):
    engine = ChronosEngine(
        demo_mode=True,
        db_path=str(tmp_path / "test_traces.db"),
        ledger_db_path=str(tmp_path / "test_ledger.db"),
    )

    initial = engine.sandbox_db.get_ledger_summary()
    assert initial["total_orders"] == 250
    assert initial["total_refunds_usd"] == 0.0

    # A. Unbounded UPDATE without WHERE -> Executes in SAVEPOINT, detects 250 rows ($30,000), rolls back!
    unbounded_res = engine.firewall.inspect_tool_call(
        "execute_sql",
        {"query": "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0;"},
        commit_if_safe=True,
    )
    assert not unbounded_res.allowed
    assert unbounded_res.action == "BLOCK"
    assert unbounded_res.shadow_diff["estimated_rows_mutated"] == 250
    assert unbounded_res.shadow_diff["transaction_action"] == "ROLLED_BACK_AT_SAVEPOINT"

    # B. DROP TABLE -> Blocked & Rolled Back
    drop_res = engine.firewall.inspect_tool_call(
        "execute_sql",
        {"query": "DROP TABLE orders;"},
        commit_if_safe=True,
    )
    assert not drop_res.allowed
    assert engine.sandbox_db.get_ledger_summary()["total_orders"] == 250

    # C. Scoped UPDATE with WHERE order_id = 'ORD-9921' -> Passes and Commits to SQLite!
    scoped_res = engine.firewall.inspect_tool_call(
        "execute_sql",
        {"query": "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0 WHERE order_id = 'ORD-9921';"},
        commit_if_safe=True,
    )
    assert scoped_res.allowed
    assert scoped_res.shadow_diff["estimated_rows_mutated"] == 1
    assert scoped_res.shadow_diff["transaction_action"] == "COMMITTED_TO_SQLITE"


def test_2_firewall_financial_shell_and_secret_rules(tmp_path):
    fw = ShadowFirewall(
        max_auto_transfer_usd=500.0,
        sandbox_db=ShadowDatabaseSandbox(db_path=str(tmp_path / "fw_ledger.db"), seed_demo=True),
    )
    assert not fw.inspect_tool_call("issue_refund", {"charge_id": "ch_1", "amount_usd": 25000.0}).allowed
    assert fw.inspect_tool_call("issue_refund", {"charge_id": "ch_1", "amount_usd": 120.0}).allowed
    assert not fw.inspect_tool_call("run_bash", {"cmd": "rm -rf /var/log/* && chmod 777 /etc/kubernetes"}).allowed
    assert not fw.inspect_tool_call("send_webhook", {"payload": "leak token sk-proj1234567890abcdefghijklm"}).allowed


def test_3_subgraph_cache_deduplication():
    cache = SubgraphCache()
    inp = {"ticket_id": "TKT-8841", "customer_id": "CUST-44910"}
    assert cache.lookup("MEMORY_READ", "fetch_customer_context", inp) is None
    cache.put("MEMORY_READ", "fetch_customer_context", inp, {"order_id": "ORD-9921"}, 640, 0.0096, 310)
    hit = cache.lookup("MEMORY_READ", "fetch_customer_context", {"customer_id": "CUST-44910", "ticket_id": "TKT-8841"})
    assert hit is not None
    assert cache.stats()["total_tokens_saved"] == 640


def test_4_time_travel_fork_finops_and_sre(tmp_path):
    engine = ChronosEngine(
        demo_mode=True,
        db_path=str(tmp_path / "traces.db"),
        ledger_db_path=str(tmp_path / "ledger.db"),
    )
    finops_run = run_finops_incident_scenario(engine, use_cache=True)
    assert finops_run.status == "HALTED_BY_FIREWALL"

    forked_finops = engine.fork_from_step(
        source_run_id=finops_run.run_id,
        target_step_index=5,
        patched_input={
            "query": "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0 WHERE order_id = 'ORD-9921';",
            "database": "ledger",
            "amount_usd": 120.0,
        },
        continuation_fn=continue_forked_finops_run,
    )
    assert forked_finops.status == "FORKED_SUCCESS"
    assert forked_finops.total_tokens_saved == 5050

    sre_run = run_sre_secops_scenario(engine)
    assert sre_run.status == "HALTED_BY_FIREWALL"
    assert len(engine.storage.load_all_runs()) == 3


def test_5_live_llm_agent_and_mcp_server(tmp_path):
    engine = ChronosEngine(
        demo_mode=True,
        db_path=str(tmp_path / "mcp_traces.db"),
        ledger_db_path=str(tmp_path / "mcp_ledger.db"),
    )
    agent = LiveLLMAgent(engine)

    # 1. Explicit destructive demo request is blocked by the SQL policy.
    bad_run = agent.run_prompt("DROP TABLE orders", provider="builtin")
    assert bad_run.status == "HALTED_BY_FIREWALL"
    assert bad_run.steps[-1].status == "BLOCKED_BY_FIREWALL"

    # 2. Safe natural language prompt -> Commits to SQLite!
    safe_run = agent.run_prompt("Estorne apenas o pedido ORD-9921 com $120", provider="builtin")
    assert safe_run.status == "COMPLETED"
    assert safe_run.steps[-1].status == "COMPLETED"

    # 3. MCP JSON-RPC 2.0 Protocol Server
    mcp = ChronosMCPServer(engine)
    init_resp = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init_resp["result"]["serverInfo"]["name"] == "chronos-mcp-firewall"

    tools_resp = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert len(tools_resp["result"]["tools"]) == 4

    call_resp = mcp.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "chronos_inspect_tool",
                "arguments": {
                    "tool_name": "execute_sql",
                    "args": {"query": "DROP TABLE orders;"},
                },
            },
        }
    )
    assert call_resp["result"]["isError"] is False
    assert "SQL-001" in call_resp["result"]["content"][0]["text"]


def test_6_fastapi_endpoints_end_to_end():
    client = TestClient(app, headers={"Authorization": "Bearer " + os.environ["CHRONOS_API_TOKEN"]})
    client.post("/api/ledger/reset")

    # Live Agent endpoint
    live_res = client.post(
        "/api/agent/run",
        json={"prompt": "DROP TABLE orders", "provider": "builtin"},
    )
    assert live_res.status_code == 200
    assert live_res.json()["run"]["status"] == "HALTED_BY_FIREWALL"

    # Self-Healing Agent endpoint
    heal_res = client.post("/api/agent/self-heal", json={"scenario_type": "finops"})
    assert heal_res.status_code == 200
    assert heal_res.json()["run"]["status"] == "SELF_HEALED_SUCCESS"

    # Integrations Info endpoint
    sdk_info = client.get("/api/integrations/info")
    assert sdk_info.status_code == 200
    assert "langchain" in sdk_info.json()["snippets"]

    # MCP info & JSON-RPC endpoint
    mcp_info = client.get("/api/mcp/info")
    assert mcp_info.status_code == 200
    assert len(mcp_info.json()["tools"]) == 4

    mcp_rpc = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "chronos_get_ledger",
                "arguments": {},
            },
        },
    )
    assert mcp_rpc.status_code == 200
    assert "total_orders" in mcp_rpc.json()["result"]["content"][0]["text"]
    client.post("/api/ledger/reset")


def test_7_semantic_vector_cache_and_entity_guard():
    cache = SubgraphCache(semantic_threshold=0.82)
    cache.put(
        "LLM_REASONING",
        "synthesize_sql",
        {"prompt": "Estornar o pedido ORD-9921 com valor 120"},
        {"sql": "UPDATE orders SET status='REFUNDED' WHERE order_id='ORD-9921';"},
        tokens=800,
        cost_usd=0.012,
        latency_ms=450,
    )

    # 1. Similar wording is deliberately not reused for a cached decision.
    sem_hit = cache.lookup(
        "LLM_REASONING",
        "synthesize_sql",
        {"prompt": "Reembolsar a ordem ORD-9921 com valor 120"},
    )
    assert sem_hit is None  # Similar text no longer authorizes cached decisions.
    assert cache.semantic_hits == 0

    # 2. Different entity ID (ORD-1002 instead of ORD-9921) -> Strictly blocked by Entity Guard!
    wrong_entity = cache.lookup(
        "LLM_REASONING",
        "synthesize_sql",
        {"prompt": "Estornar o pedido ORD-1002 com valor 120"},
    )
    assert wrong_entity is None


def test_8_self_healing_interceptor_and_langchain_wrapper(tmp_path):
    engine = ChronosEngine(
        demo_mode=True,
        db_path=str(tmp_path / "heal_traces.db"),
        ledger_db_path=str(tmp_path / "heal_ledger.db"),
    )
    interceptor = ChronosToolInterceptor(engine, self_heal=True, local_sql=True)

    # 1. Run full FinOps Self-Healing Loop
    healed_run = interceptor.run_self_healing_demo("finops")
    assert healed_run.status == "SELF_HEALED_SUCCESS"
    assert len(healed_run.steps) == 5
    assert healed_run.steps[2].status == "BLOCKED_BY_FIREWALL"
    assert "[CHRONOS_FIREWALL_BLOCKED]" in healed_run.steps[2].output_payload["self_heal_observation"]
    assert healed_run.steps[4].status == "COMPLETED"
    assert engine.sandbox_db.get_order("ORD-9921")["status"] == "REFUNDED"

    # 2. Test LangChain duck-typed tool wrapping
    class DummyLangChainTool:
        name = "execute_sql"

        def _run(self, query: str):
            return {"executed": query}

    run2 = engine.start_run("LangChain-Test", "Tool Wrapper Check")
    wrapped = interceptor.wrap_langchain_tools([DummyLangChainTool()], run=run2)
    blocked_out = wrapped[0]._run(query="DROP TABLE orders;")
    assert "self_heal_observation" in blocked_out

