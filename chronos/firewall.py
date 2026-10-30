"""
ShadowFirewall — Policy-as-Code & Real SQLite SAVEPOINT Shadow Execution Simulator for AI Agents.
Checks local SQL effects and registered tool inputs before allowing execution.
"""

import re
import json
import math
from pathlib import Path
from threading import RLock
from .locking import synchronized
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
    """Fail-closed policies for the configured local ledger and registered tools.

    This is not a process sandbox. Shell commands must be explicitly allowlisted by
    a trusted caller; SQL is confined to this ledger and never validates another DB.
    """

    SQL_TOOLS = {"execute_sql", "db_query", "sql_runner"}
    FINANCIAL_TOOLS = {"issue_refund", "wire_transfer", "stripe_payout"}
    SHELL_TOOLS = {"run_bash", "shell_exec", "terminal"}

    def __init__(self, max_auto_transfer_usd=500.0, sandbox_db=None, allowed_shell_commands=()):
        self._lock = RLock()
        self.max_auto_transfer_usd = max_auto_transfer_usd
        self.sandbox_db = sandbox_db or ShadowDatabaseSandbox()
        self.allowed_shell_commands = frozenset(allowed_shell_commands)
        self.allowed_tools = self.SQL_TOOLS | self.FINANCIAL_TOOLS | self.SHELL_TOOLS | {"verify_stripe_charge"}
        self.total_blocked = 0
        self.total_inspected = 0

    @classmethod
    def is_sql(cls, tool_name, args):
        return tool_name in cls.SQL_TOOLS or "sql" in args or "query" in args

    @synchronized
    def inspect_tool_call(self, tool_name: str, args: Dict[str, Any], commit_if_safe: bool = False) -> PolicyResult:
        self.total_inspected += 1
        violations = []
        shadow_diff = {"transaction_action": "NOT_EXECUTED"}

        def deny(rule, description):
            violations.append(PolicyViolation(rule, "HIGH" if rule == "FIN-001" else "CRITICAL", description,
                "Use explicitly supported arguments and an operation within the configured policy."))

        if not isinstance(args, dict):
            deny("ARGS-001", "Tool arguments must be an object")
            args = {}
        if tool_name not in self.allowed_tools:
            deny("TOOL-001", "Unregistered tool")
        payload_str = json.dumps(args, default=str)
        if re.search(r"(sk-[a-zA-Z0-9_-]{16,}|AKIA[0-9A-Z]{16}|\b\d{3}-\d{2}-\d{4}\b)", payload_str):
            deny("SEC-001", "Potential secret or sensitive identifier in tool input")

        if tool_name in self.FINANCIAL_TOOLS or "amount_usd" in args:
            amount = args.get("amount_usd")
            if (isinstance(amount, bool) or not isinstance(amount, (int, float))
                or not math.isfinite(amount) or amount <= 0 or amount > self.max_auto_transfer_usd):
                deny("FIN-001", "Amount must be a finite positive number within the financial limit")

        if tool_name in self.SHELL_TOOLS or "cmd" in args:
            command = args.get("cmd")
            if not isinstance(command, str) or command not in self.allowed_shell_commands:
                deny("SYS-001", "Shell command is not explicitly allowlisted; no process was executed")

        if self.is_sql(tool_name, args):
            query = args.get("query", args.get("sql"))
            if not isinstance(query, str) or not query.strip():
                deny("ARGS-001", "SQL requires a non-empty query string")
            target = args.get("database")
            if target not in (None, "ledger", self.sandbox_db.db_path, Path(self.sandbox_db.db_path).name):
                deny("SQL-TARGET", "SQL policies apply only to the configured local ledger")
            # All input policies run BEFORE any SQL can be committed.
            if not violations:
                shadow_diff = self.sandbox_db.simulate_and_validate_sql(
                    query, max_rows_allowed=1, commit_if_safe=commit_if_safe,
                    max_refund_usd=self.max_auto_transfer_usd,
                )
                for error in shadow_diff["validation_errors"]:
                    rule = error.split(":", 1)[0]
                    if re.search(r"\b(DROP|TRUNCATE|ALTER|CREATE|ATTACH|DETACH|PRAGMA)\b", query, re.I):
                        rule = "SQL-001"
                    deny(rule, error)
                if not shadow_diff["safe_to_commit"] and not violations:
                    deny("SQL-ERR", "SQL validation did not approve the statement")

        allowed = not violations
        if not allowed:
            self.total_blocked += 1
        risk = 5 if allowed else (88 if all(v.rule_id == "FIN-001" for v in violations) else 99)
        return PolicyResult(allowed, risk, "ALLOW" if allowed else "BLOCK", violations, shadow_diff)
