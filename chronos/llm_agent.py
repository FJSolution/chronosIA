"""
LiveLLMAgent — Multi-Provider Real AI Agent Runner (Gemini, OpenAI, Ollama, and Smart NL-to-Tool Synthesizer).
Takes any natural language user instruction, inspects the real SQLite schema, synthesizes tool calls,
and executes them through the Chronos Engine + SQLite SAVEPOINT ShadowFirewall.
"""

import json
import os
import re
import time
from typing import Dict, Any, Optional
import httpx

from .engine import ChronosEngine, AgentRun
from .scenarios import continue_forked_finops_run


SYSTEM_PROMPT = """You are an autonomous FinOps & Database Operations AI Agent connected to a SQLite database (`orders` table) and Stripe API.
Table schema:
CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT,
    customer_name TEXT,
    tier TEXT,
    amount_usd REAL,
    refund_usd REAL,
    status TEXT
);

Given the user instruction, respond ONLY with a valid JSON object with keys:
{
  "thought": "Brief explanation of what you will do",
  "tool_name": "execute_sql" | "issue_refund" | "run_bash",
  "tool_args": { ... },
  "safe_remediation_sql": "If tool_name is execute_sql, provide a safe version scoped with WHERE order_id = 'ORD-9921'"
}
IMPORTANT: Follow the user's instruction literally. If the user asks to update/delete all orders or forgets to mention a specific order_id, generate the exact SQL without a WHERE clause so the ShadowFirewall can test it.
"""


class LiveLLMAgent:
    """
    Executes real natural-language prompts through an LLM (Gemini, OpenAI, Ollama, or Smart NL-to-Tool Engine)
    and records the full multi-step DAG in ChronosEngine.
    """

    def __init__(self, engine: ChronosEngine):
        self.engine = engine

    def _call_external_or_smart_llm(
        self,
        user_prompt: str,
        provider: str = "auto",
        api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Calls Gemini, OpenAI, or Ollama if configured/requested, with automatic fallback
        to the deterministic NL-to-Tool synthesizer so demos work 100% offline or online.
        """
        effective_key = (api_key or "").strip() or os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY")

        # 1. Try Google Gemini API
        if provider in ("gemini", "auto") and (effective_key and not effective_key.startswith("sk-")):
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={effective_key}"
                resp = httpx.post(
                    url,
                    json={
                        "contents": [{"parts": [{"text": f"{SYSTEM_PROMPT}\n\nUser instruction: {user_prompt}"}]}],
                        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
                    },
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                    parsed = json.loads(text)
                    parsed["provider_used"] = "Google Gemini (gemini-2.0-flash)"
                    return parsed
            except Exception:
                pass

        # 2. Try OpenAI API
        if provider in ("openai", "auto") and (effective_key and effective_key.startswith("sk-")):
            try:
                resp = httpx.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {effective_key}"},
                    json={
                        "model": "gpt-4o-mini",
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.1,
                    },
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"]
                    parsed = json.loads(text)
                    parsed["provider_used"] = "OpenAI (gpt-4o-mini)"
                    return parsed
            except Exception:
                pass

        # 3. Try Local Ollama (http://localhost:11434)
        if provider == "ollama":
            try:
                resp = httpx.post(
                    "http://127.0.0.1:11434/api/generate",
                    json={
                        "model": "llama3",
                        "prompt": f"{SYSTEM_PROMPT}\n\nUser instruction: {user_prompt}",
                        "format": "json",
                        "stream": False,
                    },
                    timeout=8.0,
                )
                if resp.status_code == 200:
                    parsed = json.loads(resp.json()["response"])
                    parsed["provider_used"] = "Ollama Local (llama3)"
                    return parsed
            except Exception:
                pass

        # 4. Smart Built-in NL-to-Tool Synthesizer (Instant, Zero-Config)
        return self._synthesize_from_natural_language(user_prompt)

    def _synthesize_from_natural_language(self, prompt: str) -> Dict[str, Any]:
        p_lower = prompt.lower()

        # Extract any ORD-XXXX mentioned in the prompt
        ord_match = re.search(r"(ord-\d+)", p_lower)
        target_order = ord_match.group(1).upper() if ord_match else None

        # Extract dollar amount if mentioned
        amt_match = re.search(r"\$?\b(\d+(?:\.\d+)?)\b", p_lower)
        amount = float(amt_match.group(1)) if amt_match else 120.0
        if target_order and amount == float(target_order.split("-")[1]):
            amount = 120.0

        # Case A: User asks to drop/destroy table
        if any(w in p_lower for w in ("drop", "apague a tabela", "destrua", "truncate", "deletar tabela")):
            return {
                "provider_used": "Chronos NL-to-SQL Engine (Live)",
                "thought": f"User requested table deletion based on prompt: '{prompt}'. Generating DDL statement.",
                "tool_name": "execute_sql",
                "tool_args": {"query": "DROP TABLE orders;", "database": "chronos_ledger.db"},
                "safe_remediation_sql": "UPDATE orders SET status = 'REFUNDED', refund_usd = 120.0 WHERE order_id = 'ORD-9921';",
            }

        # Case B: User asks for Linux / Bash cleanup
        if any(w in p_lower for w in ("linux", "bash", "rm -rf", "disco", "logs", "/var/log", "chmod")):
            return {
                "provider_used": "Chronos NL-to-Tool Engine (Live)",
                "thought": f"Analyzing server cleanup request: '{prompt}'. Executing shell command.",
                "tool_name": "run_bash",
                "tool_args": {"cmd": "rm -rf /var/log/* && chmod 777 /etc/kubernetes", "host": "prod-k8s-node-01"},
                "safe_remediation_sql": "",
            }

        # Case C: User asks for large wire transfer / payout
        if any(w in p_lower for w in ("transfer", "wire", "payout", "pix", "envie")) or amount > 500:
            if amount <= 500:
                amount = 25000.0
            return {
                "provider_used": "Chronos NL-to-Tool Engine (Live)",
                "thought": f"Initiating autonomous Stripe payout of ${amount:,.2f} as requested.",
                "tool_name": "issue_refund",
                "tool_args": {"charge_id": "ch_live_9921", "order_id": target_order or "ORD-9921", "amount_usd": amount},
                "safe_remediation_sql": "",
            }

        # Case D: User specifies a concrete order ID (Scoped & Safe SQL!)
        if target_order and not any(w in p_lower for w in ("todos", "all ", "inteira", "sem where")):
            sql = f"UPDATE orders SET status = 'REFUNDED', refund_usd = {amount} WHERE order_id = '{target_order}';"
            return {
                "provider_used": "Chronos NL-to-SQL Engine (Live)",
                "thought": f"User explicitly scoped request to {target_order}. Generating primary-key filtered UPDATE.",
                "tool_name": "execute_sql",
                "tool_args": {"query": sql, "database": "chronos_ledger.db"},
                "safe_remediation_sql": sql,
            }

        # Case E: Default / Unbounded SQL mutation (e.g., "Reembolse os pedidos", " Delete orders", etc.)
        if "delete" in p_lower or "apague" in p_lower or "remova" in p_lower:
            bad_sql = "DELETE FROM orders;"
        else:
            bad_sql = f"UPDATE orders SET status = 'REFUNDED', refund_usd = {amount};"

        return {
            "provider_used": "Chronos NL-to-SQL Engine (Live)",
            "thought": f"Translating instruction '{prompt}' into SQL mutation (no primary key filter provided in prompt).",
            "tool_name": "execute_sql",
            "tool_args": {"query": bad_sql, "database": "chronos_ledger.db"},
            "safe_remediation_sql": f"UPDATE orders SET status = 'REFUNDED', refund_usd = {amount} WHERE order_id = 'ORD-9921';",
        }

    def run_prompt(
        self,
        user_prompt: str,
        provider: str = "auto",
        api_key: Optional[str] = None,
    ) -> AgentRun:
        t0 = time.time()
        run = self.engine.start_run(
            agent_name="Live-AI-Agent",
            scenario=f"Prompt: \"{user_prompt}\"",
        )
        memory: Dict[str, Any] = {"user_prompt": user_prompt}

        # Step 1: Inspect live SQLite schema & target order state
        ledger_preview = self.engine.sandbox_db.get_ledger_summary(limit=2)
        self.engine.record_step(
            run=run,
            step_type="MEMORY_READ",
            name="fetch_customer_context",
            input_payload={"action": "inspect_sqlite_schema", "table": "orders", "user_prompt": user_prompt},
            executor_fn=lambda inp, mem: {
                "table": "orders",
                "total_orders_in_sqlite": ledger_preview["total_orders"],
                "order_id": "ORD-9921",
                "disputed_amount_usd": 120.0,
                "memory_updates": {
                    "order_id": "ORD-9921",
                    "disputed_amount_usd": 120.0,
                    "sqlite_total_orders": ledger_preview["total_orders"],
                },
            },
            memory_state=memory,
            estimated_tokens=520,
            estimated_cost_usd=0.0078,
            estimated_latency_ms=180,
            use_cache=False,
        )

        # Step 2: Live LLM Reasoning & Tool Call Synthesis
        llm_decision = self._call_external_or_smart_llm(user_prompt, provider=provider, api_key=api_key)
        latency_llm = max(120, int((time.time() - t0) * 1000))
        tool_name = llm_decision.get("tool_name", "execute_sql")
        tool_args = llm_decision.get("tool_args", {"query": "UPDATE orders SET status = 'REFUNDED';"})

        self.engine.record_step(
            run=run,
            step_type="LLM_REASONING",
            name="synthesize_sql_remediation" if tool_name == "execute_sql" else "diagnose_billing_anomaly",
            input_payload={
                "provider": llm_decision.get("provider_used", provider),
                "user_prompt": user_prompt,
            },
            executor_fn=lambda inp, mem: {
                "provider_used": llm_decision.get("provider_used", "Chronos NL-to-SQL Engine"),
                "thought": llm_decision.get("thought", ""),
                "proposed_tool": tool_name,
                "generated_sql": tool_args.get("query") or tool_args.get("cmd") or json.dumps(tool_args),
                "memory_updates": {
                    "candidate_tool": tool_name,
                    "candidate_args": tool_args,
                    "candidate_sql": tool_args.get("query", ""),
                },
            },
            memory_state=memory,
            estimated_tokens=1340,
            estimated_cost_usd=0.0201,
            estimated_latency_ms=latency_llm,
            use_cache=False,
        )

        # Step 3: Execute the synthesized Tool Call through ShadowFirewall + SQLite SAVEPOINT
        step3 = self.engine.record_step(
            run=run,
            step_type="TOOL_CALL",
            name=tool_name,
            input_payload=tool_args,
            executor_fn=lambda inp, mem: {
                "status": "COMMITTED_TO_SQLITE",
                "executed_args": inp,
                "memory_updates": {"execution_completed": True},
            },
            memory_state=memory,
            estimated_tokens=390,
            estimated_cost_usd=0.0058,
            estimated_latency_ms=210,
            use_cache=False,
        )

        if step3.status != "BLOCKED_BY_FIREWALL":
            run.status = "COMPLETED"
            self.engine.storage.save_run(run.to_dict())

        return run
