"""
ShadowFirewall — Policy-as-Code & Real SQLite SAVEPOINT Shadow Execution Simulator for AI Agents.
Intercepts destructive tool calls (SQL, Shell, Payments, PII) before production execution.
"""

import re
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional

from .sandbox_db import ShadowDatabaseSandbox


@dataclass
class PolicyViolation:
    rule_id: str
    severity: str  # "CRITICAL", "HIGH", "MEDIUM"
    description: str
    remediation: str


@dataclass
class PolicyResult:
    allowed: bool
    risk_score: int  # 0 to 100
    action: str  # "ALLOW", "BLOCK", "REQUIRE_APPROVAL"
    violations: List[PolicyViolation] = field(default_factory=list)
    shadow_diff: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ShadowFirewall:
    """
    Simulates blast radius in an isolated SQLite SAVEPOINT shadow state and enforces
    deterministic safety policies before an autonomous agent can mutate production state.
    """

    def __init__(self, max_auto_transfer_usd: float = 500.0, sandbox_db: Optional[ShadowDatabaseSandbox] = None):
        self.max_auto_transfer_usd = max_auto_transfer_usd
        self.sandbox_db = sandbox_db or ShadowDatabaseSandbox()
        self.total_blocked = 0
        self.total_inspected = 0

    def inspect_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        commit_if_safe: bool = False,
    ) -> PolicyResult:
        self.total_inspected += 1
        violations: List[PolicyViolation] = []
        shadow_diff: Dict[str, Any] = {}
        risk_score = 5

        payload_str = " ".join(f"{k}={v}" for k, v in args.items())

        # 1. Real SQLite SAVEPOINT Shadow Execution for SQL Tools
        if tool_name in ("execute_sql", "db_query", "sql_runner") or "sql" in args or "query" in args:
            query = str(args.get("query") or args.get("sql") or payload_str).strip()
            upper_q = query.upper()

            # Run inside real SQLite SAVEPOINT!
            shadow_diff = self.sandbox_db.simulate_and_validate_sql(
                query=query,
                max_rows_allowed=1,
                commit_if_safe=commit_if_safe,
            )
            rows_mutated = shadow_diff.get("estimated_rows_mutated", 0)
            refund_delta = shadow_diff.get("financial_refund_delta_usd", 0.0)

            if re.search(r"\b(DROP\s+TABLE|DROP\s+DATABASE|TRUNCATE\s+TABLE)\b", upper_q) or shadow_diff.get("table_destroyed"):
                violations.append(
                    PolicyViolation(
                        rule_id="SQL-001",
                        severity="CRITICAL",
                        description=f"DDL destrutivo detectado no SAVEPOINT (Destruiria {rows_mutated} pedidos reais!). Rollback automático executado.",
                        remediation="Use soft-delete ou UPDATE com cláusula WHERE explícita na chave primária.",
                    )
                )
                risk_score = 99

            elif (
                re.search(r"\b(DELETE\s+FROM|UPDATE\s+\w+\s+SET)\b", upper_q)
                and ("WHERE" not in upper_q or rows_mutated > 1)
            ):
                violations.append(
                    PolicyViolation(
                        rule_id="SQL-002",
                        severity="CRITICAL",
                        description=(
                            f"Mutação em massa detectada no SAVEPOINT SQLite: afetou {rows_mutated} pedidos reais "
                            f"(Impacto de ${abs(refund_delta):,.2f}) via SCAN completo sem filtro primário! Rollback automático executado."
                        ),
                        remediation="Adicione cláusula WHERE explícita filtrando o ID do pedido (ex: WHERE order_id = 'ORD-9921').",
                    )
                )
                risk_score = max(risk_score, 96)

            elif shadow_diff.get("execution_error"):
                violations.append(
                    PolicyViolation(
                        rule_id="SQL-ERR",
                        severity="MEDIUM",
                        description=f"Erro de sintaxe/schema no SAVEPOINT SQLite: {shadow_diff['execution_error']}",
                        remediation="Verifique o nome das colunas na tabela orders (order_id, status, refund_usd).",
                    )
                )
                risk_score = max(risk_score, 70)

        # 2. Financial / Stripe Wire Transfer Guardrails
        if tool_name in ("issue_refund", "wire_transfer", "stripe_payout") or "amount_usd" in args:
            amount = float(args.get("amount_usd", 0.0))
            if amount > self.max_auto_transfer_usd:
                violations.append(
                    PolicyViolation(
                        rule_id="FIN-001",
                        severity="HIGH",
                        description=f"Transferência autônoma (${amount:,.2f}) excede o limite da política (${self.max_auto_transfer_usd:,.2f}).",
                        remediation=f"Limite o estorno automático a <= ${self.max_auto_transfer_usd:,.2f} ou envie para aprovação humana.",
                    )
                )
                risk_score = max(risk_score, 88)
                shadow_diff = {
                    "target": "stripe_treasury.main_vault",
                    "delta_usd": -amount,
                    "policy_ceiling_usd": self.max_auto_transfer_usd,
                    "transaction_action": "BLOCKED_BEFORE_API_CALL",
                    "reversible": False,
                }
            elif not shadow_diff:
                shadow_diff = {
                    "target": "stripe_treasury.main_vault",
                    "delta_usd": -amount,
                    "policy_ceiling_usd": self.max_auto_transfer_usd,
                    "transaction_action": "AUTHORIZED",
                    "reversible": True,
                }

        # 3. Shell / Filesystem Destructive Commands
        if tool_name in ("run_bash", "shell_exec", "terminal") or "cmd" in args:
            cmd = str(args.get("cmd") or payload_str)
            if re.search(r"(rm\s+-rf|mkfs|dd\s+if=|chmod\s+777|> /dev/)", cmd):
                violations.append(
                    PolicyViolation(
                        rule_id="SYS-001",
                        severity="CRITICAL",
                        description="Comando Bash destrutivo bloqueado no container Shadow (rm -rf / chmod 777).",
                        remediation="Utilize logrotate e journalctl --vacuum-size para limpeza segura de logs.",
                    )
                )
                risk_score = 99
                shadow_diff = {
                    "target": "host_filesystem (/var/log, /etc/kubernetes)",
                    "command": cmd,
                    "blast_radius": "Deleção recursiva de logs de auditoria + escalação de privilégio (chmod 777)",
                    "transaction_action": "BLOCKED_IN_NAMESPACE",
                    "reversible": False,
                }

        # 4. PII / Secret Exfiltration Check
        if re.search(r"(sk-[a-zA-Z0-9]{16,}|AKIA[0-9A-Z]{16}|\b\d{3}-\d{2}-\d{4}\b)", payload_str):
            violations.append(
                PolicyViolation(
                    rule_id="SEC-001",
                    severity="CRITICAL",
                    description="Credencial de API, chave AWS ou dado sensível detectado no payload de saída.",
                    remediation="Mascarar segredos usando referências do Chronos Vault antes de chamar ferramentas externas.",
                )
            )
            risk_score = max(risk_score, 96)

        allowed = len(violations) == 0
        if not allowed:
            self.total_blocked += 1

        action = "ALLOW" if allowed else ("BLOCK" if risk_score >= 90 else "REQUIRE_APPROVAL")

        return PolicyResult(
            allowed=allowed,
            risk_score=risk_score,
            action=action,
            violations=violations,
            shadow_diff=shadow_diff,
        )
