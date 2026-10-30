"""Regression cases from the audit; all databases and credentials are isolated."""

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from chronos import ChronosEngine, ChronosSDK, ChronosToolInterceptor, LiveLLMAgent, SubgraphCache


@pytest.fixture
def engine():
    instance = ChronosEngine(db_path=":memory:", ledger_db_path=":memory:", demo_mode=True)
    yield instance
    instance.storage._conn.close()
    instance.sandbox_db._conn.close()


def inspect(engine, query, **args):
    return engine.firewall.inspect_tool_call("execute_sql", {"query": query, **args}, commit_if_safe=True)


@pytest.mark.parametrize("query", [
    "WITH ids AS (SELECT order_id FROM orders) UPDATE orders SET status='REFUNDED' WHERE order_id IN (SELECT order_id FROM ids)",
    "WITH unused AS (SELECT 1) UPDATE orders SET status='REFUNDED' WHERE 1=1",
    "UPDATE orders SET status='REFUNDED' WHERE 1=1 RETURNING order_id",
    "UPDATE orders SET status='WHERE harmless' /* WHERE order_id='ORD-9921' */",
    "DELETE FROM orders WHERE 1=1 RETURNING order_id",
    "INSERT INTO orders SELECT order_id || '-copy',customer_id,customer_name,tier,amount_usd,refund_usd,status FROM orders",
    "DROP TABLE orders",
    "ALTER TABLE orders RENAME TO lost",
    "CREATE TABLE extra (value TEXT)",
    "ATTACH DATABASE ':memory:' AS other",
    "PRAGMA writable_schema=ON",
    "COMMIT",
    "RELEASE SAVEPOINT chronos_shadow_sp",
    "SELECT load_extension('untrusted')",
    "SELECT * FROM sqlite_master",
    "UPDATE orders SET status='REFUNDED' WHERE order_id='ORD-9921'; DELETE FROM orders",
])
def test_unsafe_sql_never_changes_ledger(engine, query):
    before = engine.sandbox_db.get_ledger_summary(limit=300)
    result = inspect(engine, query)
    assert not result.allowed
    assert engine.sandbox_db.get_ledger_summary(limit=300) == before
    assert not engine.sandbox_db._conn.in_transaction


def test_cte_count_does_not_use_cursor_rowcount(engine):
    result = inspect(engine, "WITH unused AS (SELECT 1) UPDATE orders SET status='REFUNDED' WHERE 1=1")
    assert not result.allowed
    assert result.shadow_diff["estimated_rows_mutated"] == 250


def test_safe_returning_and_select(engine):
    result = inspect(engine, "UPDATE orders SET refund_usd=120 WHERE order_id='ORD-9921' RETURNING order_id,refund_usd")
    assert result.allowed
    assert result.shadow_diff["estimated_rows_mutated"] == 1
    assert result.shadow_diff["result_rows"] == [{"order_id": "ORD-9921", "refund_usd": 120.0}]
    result = inspect(engine, "SELECT refund_usd FROM orders WHERE order_id='ORD-9921'")
    assert result.allowed
    assert result.shadow_diff["result_rows"] == [{"refund_usd": 120.0}]


@pytest.mark.parametrize("extra", [{"amount_usd": 900}, {"note": "sk-example1234567890example"}, {"database": "remote-production"}])
def test_all_policies_run_before_sql_commit(engine, extra):
    result = inspect(engine, "UPDATE orders SET refund_usd=100 WHERE order_id='ORD-9921'", **extra)
    assert not result.allowed
    assert engine.sandbox_db.get_order("ORD-9921")["refund_usd"] == 0


@pytest.mark.parametrize("refund", ["9000", "-1", "1e999", "'not money'", "121"])
def test_sql_financial_effect_checked_without_amount_argument(engine, refund):
    assert not inspect(engine, f"UPDATE orders SET refund_usd={refund} WHERE order_id='ORD-9921'").allowed
    assert engine.sandbox_db.get_order("ORD-9921")["refund_usd"] == 0


@pytest.mark.parametrize("amount", [None, -1, 0, True, "120", float("nan"), float("inf"), 25000])
def test_invalid_financial_arguments_fail_closed(engine, amount):
    assert not engine.firewall.inspect_tool_call("wire_transfer", {"amount_usd": amount}).allowed


@pytest.mark.parametrize("command", ["rm -fr /var/log/*", "rm -r -f /var/log/*", "python -c 'print(1)'", "echo $(rm -rf /tmp/anything)", "powershell Remove-Item anything"])
def test_shell_requires_explicit_allowlist(engine, command):
    assert not engine.firewall.inspect_tool_call("run_bash", {"cmd": command}).allowed


def test_sdk_binds_positional_mixed_and_default_arguments(engine):
    sdk = ChronosSDK(engine)
    calls = []

    @sdk.step(name="wire_transfer")
    def transfer(amount_usd=25000, destination="demo"):
        calls.append((amount_usd, destination))
        return {"ok": True}

    for args, kwargs in [((25000,), {}), ((), {}), ((25000,), {"destination": "demo"})]:
        with sdk.trace_run("audit", "binding") as run:
            transfer(*args, **kwargs)
        assert run.status == "HALTED_BY_FIREWALL"
    assert calls == []
    with sdk.trace_run("audit", "binding") as run:
        transfer(120, destination="demo")
    assert calls == [(120, "demo")]
    persisted = next(r for r in engine.storage.load_all_runs() if r["run_id"] == run.run_id)
    assert persisted["status"] == "COMPLETED"
    with pytest.raises(RuntimeError):
        transfer(120)


def test_interceptor_binds_defaults_and_rejects_unknown_tools(engine):
    interceptor = ChronosToolInterceptor(engine, self_heal=False)
    calls = []

    def transfer(amount_usd=25000):
        calls.append(amount_usd)
        return {}

    run = engine.start_run("audit", "interceptor")
    wrapped = interceptor.wrap_callable(transfer, run, {}, tool_name="wire_transfer")
    wrapped()
    assert not calls
    with pytest.raises(ValueError, match="registered"):
        interceptor.intercept_openai_tool_call(engine.start_run("audit", "unknown"),
            {"function": {"name": "missing", "arguments": "{}"}}, {})


def test_halted_runs_cannot_execute_callbacks(engine):
    run = engine.start_run("audit", "halt")
    engine.record_step(run, "TOOL_CALL", "issue_refund", {"amount_usd": 25000}, lambda i, m: {}, {})
    called = []
    with pytest.raises(ValueError, match="RUNNING"):
        engine.record_step(run, "LLM_REASONING", "next", {}, lambda i, m: called.append(True) or {}, {})
    assert called == []


def test_local_sql_runs_once_and_external_callback_is_not_called(engine):
    sdk = ChronosSDK(engine)
    calls = []

    @sdk.step(name="execute_sql", local_sql=True)
    def sql(query):
        calls.append(True)
        return {}

    with sdk.trace_run("audit", "sql"):
        for _ in range(2):
            sql("UPDATE orders SET refund_usd=refund_usd+1 WHERE order_id='ORD-9921'")
    assert calls == []
    assert engine.sandbox_db.get_order("ORD-9921")["refund_usd"] == 2

    @sdk.step(name="execute_sql")
    def external_sql(query):
        calls.append(True)
        return {}

    with sdk.trace_run("audit", "external") as run:
        external_sql("UPDATE orders SET refund_usd=99 WHERE order_id='ORD-9921'")
    assert run.status == "HALTED_BY_FIREWALL"
    assert engine.sandbox_db.get_order("ORD-9921")["refund_usd"] == 2
    assert calls == []


def test_cache_uses_memory_version_and_opt_in(engine):
    run = engine.start_run("audit", "cache")
    calls = []

    def calculate(i, m):
        calls.append(m["value"])
        return {"result": m["value"]}

    for value, version, expected in [(10, "1", "COMPLETED"), (10, "1", "CACHED_HIT"), (99, "1", "COMPLETED"), (99, "2", "COMPLETED")]:
        step = engine.record_step(run, "LLM_REASONING", "calc", {}, calculate, {"value": value}, use_cache=True, cache_version=version)
        assert step.status == expected
        assert step.output_payload["result"] == value
    assert calls == [10, 99, 99]


def test_cache_negation_expiration_and_copy_isolation():
    cache = SubgraphCache()
    original = {"refund": True}
    request = {"prompt": "Reembolsar o pedido ORD-9921 com valor 120"}
    cache.put("LLM_REASONING", "decision", request, original, 1, 0, 0)
    original["refund"] = False
    assert cache.lookup("LLM_REASONING", "decision", request).output_payload["refund"] is True
    assert cache.lookup("LLM_REASONING", "decision", {"prompt": "Nao reembolsar o pedido ORD-9921 com valor 120"}) is None
    with pytest.raises(ValueError):
        cache.lookup("LLM_REASONING", "decision", request, allow_semantic=True)
    cache.ttl_seconds = 0
    assert cache.lookup("LLM_REASONING", "decision", request) is None


def test_tools_do_not_skip_side_effects_on_cache_hits(engine):
    sdk = ChronosSDK(engine)
    calls = []

    @sdk.step(name="issue_refund", use_cache=True)
    def refund(amount_usd):
        calls.append(amount_usd)
        return {"done": True}

    with sdk.trace_run("audit", "payments"):
        refund(120)
        refund(120)
    assert calls == [120, 120]


def test_restart_recovers_history_without_reset(tmp_path):
    settings = dict(db_path=str(tmp_path / "traces.db"), ledger_db_path=str(tmp_path / "ledger.db"), demo_mode=True)
    a = ChronosEngine(**settings)
    inspect(a, "UPDATE orders SET refund_usd=100 WHERE order_id='ORD-9921'")
    run = a.start_run("audit", "restart")
    a.record_step(run, "MEMORY_READ", "state", {}, lambda i, m: {"memory_updates": {"key": "value"}}, {})
    a.storage._conn.close()
    a.sandbox_db._conn.close()
    b = ChronosEngine(**settings)
    try:
        assert b.sandbox_db.get_order("ORD-9921")["refund_usd"] == 100
        assert b.runs[run.run_id].status == "INTERRUPTED"
        assert b.runs[run.run_id].steps[0].memory_snapshot == {"key": "value"}
        with pytest.raises(ValueError, match="continuation"):
            b.fork_from_step(run.run_id, 1, {})
        with pytest.raises(ValueError, match="target step"):
            b.fork_from_step(run.run_id, 1, {}, lambda *args: None)
    finally:
        b.storage._conn.close()
        b.sandbox_db._conn.close()


def test_concurrent_sql_transactions_are_isolated(engine):
    def execute(number):
        if number % 2:
            return inspect(engine, "UPDATE orders SET refund_usd=refund_usd+1 WHERE order_id='ORD-9921'").allowed
        return inspect(engine, "UPDATE orders SET status='REFUNDED' WHERE 1=1").allowed

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(execute, range(40)))
    assert results == [bool(n % 2) for n in range(40)]
    assert engine.sandbox_db.get_order("ORD-9921")["refund_usd"] == 20
    assert engine.sandbox_db.get_ledger_summary()["status_distribution"].get("REFUNDED", 0) == 0


def test_non_demo_database_is_never_seeded_or_reset():
    instance = ChronosEngine(db_path=":memory:", ledger_db_path=":memory:")
    try:
        assert instance.sandbox_db.get_ledger_summary()["total_orders"] == 0
        with pytest.raises(PermissionError):
            instance.sandbox_db.reset_seed_data()
    finally:
        instance.sandbox_db._conn.close()
        instance.storage._conn.close()


def test_builtin_reads_do_not_mutate_and_ambiguous_commands_fail(engine):
    agent = LiveLLMAgent(engine)
    run = agent.run_prompt("Consulte o pedido ORD-9921", provider="builtin")
    assert run.steps[-1].output_payload["rows"][0]["order_id"] == "ORD-9921"
    assert engine.sandbox_db.get_order("ORD-9921")["refund_usd"] == 0
    for prompt in ["Nao reembolsar ORD-9921 com 120", "Reembolse todos com 120", "Envie 120 via pix", "bom dia", "Estorne ORD-9921"]:
        with pytest.raises(ValueError):
            agent.run_prompt(prompt, provider="builtin")
    run = agent.run_prompt("Estorne ORD-9921 com 45", provider="builtin")
    assert run.status == "COMPLETED"
    assert engine.sandbox_db.get_order("ORD-9921")["refund_usd"] == 45


def test_provider_failure_does_not_fall_back_to_mutation(engine, monkeypatch):
    import chronos.llm_agent as module
    monkeypatch.setattr(module.httpx, "post", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(ValueError, match="no fallback"):
        LiveLLMAgent(engine).run_prompt("Estorne ORD-9921 com 120", "openai", "sk-test")
    assert engine.sandbox_db.get_order("ORD-9921")["refund_usd"] == 0
    assert list(engine.runs.values())[-1].status == "FAILED"


def test_api_authentication_and_permissions(monkeypatch):
    import server
    client = TestClient(server.app)
    for path in ["/api/overview", "/api/ledger", "/api/mcp/info", "/openapi.json"]:
        assert client.get(path).status_code == 401
    assert client.post("/api/ledger/reset").status_code == 401
    assert client.post("/mcp", json={}).status_code == 401
    monkeypatch.setenv("CHRONOS_READ_TOKEN", "reader-" * 8)
    reader = {"Authorization": "Bearer " + "reader-" * 8}
    assert client.get("/api/ledger", headers=reader).status_code == 200
    assert client.post("/api/ledger/reset", headers=reader).status_code == 403
    admin = {"Authorization": "Bearer " + os.environ["CHRONOS_API_TOKEN"]}
    assert client.get("/api/overview", headers=admin).status_code == 200
    monkeypatch.setattr(server.engine, "demo_mode", False)
    assert client.post("/api/ledger/reset", headers=admin).status_code == 403
    assert client.post("/api/runs/simulate", json={}, headers=admin).status_code == 403
    monkeypatch.delenv("CHRONOS_API_TOKEN")
    assert client.get("/api/ledger", headers=admin).status_code == 503


def test_non_json_sql_output_and_blob_columns_rollback(engine):
    query = "UPDATE orders SET customer_name=CAST('test' AS BLOB) WHERE order_id='ORD-9921' RETURNING customer_name"
    assert not inspect(engine, query).allowed
    assert engine.sandbox_db.get_order("ORD-9921")["customer_name"] == "Acme Corp Enterprise"
    assert not inspect(engine, "SELECT CAST(order_id AS BLOB) FROM orders LIMIT 1").allowed


def test_negative_refund_is_not_reinterpreted_as_positive(engine):
    run = LiveLLMAgent(engine).run_prompt("Estorne ORD-9921 com -5", provider="builtin")
    assert run.status == "HALTED_BY_FIREWALL"
    assert engine.sandbox_db.get_order("ORD-9921")["refund_usd"] == 0


def test_auto_provider_uses_key_source_not_token_prefix(engine, monkeypatch):
    import chronos.llm_agent as module
    monkeypatch.setenv("OPENAI_API_KEY", "nonstandard-test-format")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    calls = []

    def offline(url, **kwargs):
        calls.append(url)
        raise RuntimeError("offline")

    monkeypatch.setattr(module.httpx, "post", offline)
    with pytest.raises(ValueError):
        LiveLLMAgent(engine).run_prompt("Consulte ORD-9921", provider="auto")
    assert calls == ["https://api.openai.com/v1/chat/completions"]


def test_async_capable_objects_cannot_bypass_interceptor(engine):
    class AsyncTool:
        name = "wire_transfer"
        def _run(self, amount_usd):
            return {}
        async def _arun(self, amount_usd):
            return {}

    with pytest.raises(TypeError, match="Async"):
        ChronosToolInterceptor(engine).wrap_langchain_tools([AsyncTool()], engine.start_run("audit", "async"))


def test_mcp_sql_records_trace_and_invalid_requests_fail(engine):
    from chronos import ChronosMCPServer
    mcp = ChronosMCPServer(engine)
    response = mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "chronos_execute_safe_sql", "arguments": {"query": "UPDATE orders SET refund_usd=20 WHERE order_id='ORD-9921'"}}})
    output = json.loads(response["result"]["content"][0]["text"])
    assert output["allowed"] is True
    assert engine.runs[output["run_id"]].status == "COMPLETED"
    assert len(engine.runs[output["run_id"]].steps) == 1
    assert mcp.handle_jsonrpc({"method": 123})["error"]["code"] == -32600


def test_unresolved_self_heal_is_not_marked_completed(engine):
    sdk = ChronosSDK(engine)

    @sdk.step(name="wire_transfer", self_heal=True)
    def transfer(amount_usd):
        pytest.fail("Blocked transfer must not execute")

    with sdk.trace_run("audit", "unresolved") as run:
        transfer(25000)
    assert run.status == "HALTED_BY_FIREWALL"
    assert engine.storage.load_all_runs()[0]["status"] == "HALTED_BY_FIREWALL"


def test_hidden_trigger_effects_are_rejected(engine):
    engine.sandbox_db._conn.execute("""CREATE TRIGGER unsafe_effect AFTER UPDATE ON orders
        BEGIN UPDATE orders SET refund_usd=1; END""")
    before = engine.sandbox_db.get_ledger_summary(limit=300)
    assert not inspect(engine, "UPDATE orders SET refund_usd=20 WHERE order_id='ORD-9921'").allowed
    assert engine.sandbox_db.get_ledger_summary(limit=300) == before
