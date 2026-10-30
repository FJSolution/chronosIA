"""
LiveLLMAgent — Multi-Provider Real AI Agent Runner (Gemini, OpenAI, Ollama, and Smart NL-to-Tool Synthesizer).
Takes any natural language user instruction, inspects the real SQLite schema, synthesizes tool calls,
and executes them through the Chronos Engine + SQLite SAVEPOINT ShadowFirewall.
"""

import json
import os
import re
import unicodedata
from typing import Dict, Any, Optional
import httpx

from .engine import ChronosEngine, AgentRun


SYSTEM_PROMPT = """You are an autonomous FinOps & Database Operations AI Agent connected only to a local SQLite ledger (`orders` table). Shell and payments have no live executor.
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
  "safe_remediation_sql": "Do not invent a target order, amount or action"
}
IMPORTANT: Never invent an entity or amount. Read requests must produce SELECT. Ambiguous or negated mutation requests must return an error field instead of a tool call.
"""


class LiveLLMAgent:
    """
    Executes real natural-language prompts through an LLM (Gemini, OpenAI, Ollama, or Smart NL-to-Tool Engine)
    and records its sequential execution in ChronosEngine.
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
        Calls Gemini, OpenAI, or Ollama if configured/requested, when explicitly selected or configured. With no external credentials, use
        the restricted local parser. Provider failures never execute fallback actions.
        """
        if provider not in {"auto", "builtin", "gemini", "openai", "ollama"}:
            raise ValueError("Unsupported provider")
        if provider == "auto":
            if api_key:
                raise ValueError("Choose a provider explicitly when supplying an API key")
            if os.environ.get("GEMINI_API_KEY"):
                provider = "gemini"
            elif os.environ.get("OPENAI_API_KEY"):
                provider = "openai"
            else:
                provider = "builtin"
        if provider == "builtin":
            return self._synthesize_from_natural_language(user_prompt)
        if provider == "openai":
            effective_key = (api_key or "").strip() or os.environ.get("OPENAI_API_KEY")
        elif provider == "gemini":
            effective_key = (api_key or "").strip() or os.environ.get("GEMINI_API_KEY")
        else:
            effective_key = (api_key or "").strip() or os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY")

        # 1. Try Google Gemini API
        if provider == "gemini" and effective_key:
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
        if provider == "openai" and effective_key:
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

        if provider != "auto" or effective_key:
            raise ValueError("Provider unavailable or invalid response; no fallback action was executed")
        # Offline parsing is used only when no external provider is configured.
        return self._synthesize_from_natural_language(user_prompt)

    def _synthesize_from_natural_language(self, prompt: str) -> Dict[str, Any]:
        text = "".join(c for c in unicodedata.normalize("NFKD", prompt.lower()) if not unicodedata.combining(c)).strip()
        if re.search(r"\b(nao|not|never|nunca|don.t)\b", text):
            raise ValueError("Negated requests require clarification; no action generated")
        orders = re.findall(r"\bord-\d+\b", text)
        if len(set(orders)) > 1:
            raise ValueError("Specify exactly one target order")
        target = orders[0].upper() if orders else None
        provider = "Built-in restricted parser (no LLM)"
        if re.match(r"^(consulte|consultar|mostre|mostrar|liste|listar|verifique|verificar|show|list|select)\b", text):
            sql = "SELECT * FROM orders" + (f" WHERE order_id = '{target}'" if target else " ORDER BY order_id LIMIT 15")
        elif re.match(r"^(reembolse|reembolsar|estorne|estornar|refund)\b", text):
            without_ids = re.sub(r"\bord-\d+\b", "", text)
            amounts = re.findall(r"(?<![\w.,])[+-]?\d+(?:[.,]\d{1,2})?(?![\w.,])", without_ids)
            if len(amounts) != 1:
                raise ValueError("Specify one explicit refund amount")
            amount = float(amounts[0].replace(",", "."))
            if not target:
                raise ValueError("Specify an explicit order ID for a refund")
            sql = f"UPDATE orders SET status='REFUNDED', refund_usd={amount} WHERE order_id='{target}'"
        elif self.engine.demo_mode and re.match(r"^(drop|apague|delete|destrua)\b", text):
            sql = "DROP TABLE orders" if "tabela" in text or "drop" in text else "DELETE FROM orders"
        else:
            raise ValueError("Unsupported or ambiguous instruction; use an explicit read or scoped refund")
        return {"provider_used": provider, "thought": "Explicit local ledger operation", "tool_name": "execute_sql", "tool_args": {"query": sql, "database": "ledger"}}

    def run_prompt(self, user_prompt: str, provider: str = "auto", api_key: Optional[str] = None) -> AgentRun:
        run = self.engine.start_run("Live-AI-Agent", f'Prompt: "{user_prompt}"')
        memory = {"user_prompt": user_prompt}
        decision = {}

        def reason(inp, mem):
            value = self._call_external_or_smart_llm(user_prompt, provider, api_key)
            if (not isinstance(value, dict) or value.get("error") or
                value.get("tool_name") not in {"execute_sql", "issue_refund", "run_bash"} or
                not isinstance(value.get("tool_args"), dict)):
                raise ValueError("Provider did not return a supported tool decision")
            decision.update(value)
            return value

        self.engine.record_step(run, "LLM_REASONING", "synthesize_sql_remediation",
            {"provider": provider, "user_prompt": user_prompt}, reason, memory, use_cache=False)

        def unsupported_or_demo(inp, mem):
            if not self.engine.demo_mode:
                raise ValueError("No live executor configured for this tool")
            return {"status": "SIMULATED", "executed": False, "proposed_args": inp}

        self.engine.record_step(run, "TOOL_CALL", decision["tool_name"], decision["tool_args"],
            unsupported_or_demo, memory, use_cache=False)
        if run.status == "RUNNING":
            run.status = "COMPLETED"
        self.engine.storage.save_run(run.to_dict())
        return run
