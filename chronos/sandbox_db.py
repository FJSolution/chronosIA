"""
ShadowDatabaseSandbox — Real SQLite Transaction Sandbox using SAVEPOINT / ROLLBACK
and EXPLAIN QUERY PLAN inspection.
Executes agent SQL inside an isolated savepoint, measures real row mutations and financial
blast radius, and rolls back automatically if safety thresholds are violated.
"""

import json
import sqlite3
import math
import re
import time
from threading import RLock
from .locking import synchronized
from pathlib import Path
from typing import Dict, Any, List, Optional


class ShadowDatabaseSandbox:
    """
    Manages a real SQLite database (`orders` and `customers` tables) representing
    the configured local ledger, and executes candidate SQL inside a SAVEPOINT to compute
    the exact blast radius before deciding whether to COMMIT or ROLLBACK.
    """

    def __init__(self, db_path: Optional[str] = None, seed_demo: bool = False):
        self._lock = RLock()
        self.demo_mode = seed_demo
        self.db_path = db_path or str(Path(__file__).parent.parent / "chronos_ledger.db")
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None, cached_statements=0)
        self._conn.row_factory = sqlite3.Row
        self._init_and_seed()

    def _init_and_seed(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        is_new = not cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                tier TEXT NOT NULL,
                amount_usd REAL NOT NULL,
                refund_usd REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL
            );
            """
        )
        cur.execute("SELECT COUNT(*) as cnt FROM orders;")
        count = cur.fetchone()["cnt"]
        if is_new and count == 0 and self.demo_mode:
            self.reset_seed_data()

    @synchronized
    def reset_seed_data(self) -> None:
        if not self.demo_mode:
            raise PermissionError("Reset is only available in explicit demo mode")
        cur = self._conn.cursor()
        cur.execute("BEGIN;")
        cur.execute("DELETE FROM orders;")

        # Seed the target disputed order ORD-9921 plus 249 other fictitious demo orders (250 total)
        seed_rows = [
            ("ORD-9921", "CUST-44910", "Acme Corp Enterprise", "ENTERPRISE_VIP", 120.0, 0.0, "DISPUTED"),
            ("ORD-9922", "CUST-10291", "Globex Cloud Ltd", "ENTERPRISE", 4500.0, 0.0, "COMPLETED"),
            ("ORD-9923", "CUST-38192", "Stark FinTech", "ENTERPRISE_VIP", 12800.0, 0.0, "COMPLETED"),
            ("ORD-9924", "CUST-55102", "Initech Systems", "PRO", 890.0, 0.0, "COMPLETED"),
            ("ORD-9925", "CUST-77819", "Wayne Ventures", "ENTERPRISE_VIP", 9400.0, 0.0, "COMPLETED"),
            ("ORD-9926", "CUST-88310", "Umbrella BioLabs", "ENTERPRISE", 3200.0, 0.0, "COMPLETED"),
            ("ORD-9927", "CUST-99104", "Cyberdyne AI", "PRO", 640.0, 0.0, "COMPLETED"),
            ("ORD-9928", "CUST-21940", "Hooli Search", "ENTERPRISE", 5100.0, 0.0, "COMPLETED"),
        ]
        for i in range(9929, 10171):
            seed_rows.append(
                (
                    f"ORD-{i}",
                    f"CUST-{i * 3}",
                    f"Customer Account #{i}",
                    "PRO" if i % 2 == 0 else "ENTERPRISE",
                    float((i % 15 + 1) * 150.0),
                    0.0,
                    "COMPLETED",
                )
            )

        cur.executemany(
            "INSERT INTO orders (order_id, customer_id, customer_name, tier, amount_usd, refund_usd, status) VALUES (?, ?, ?, ?, ?, ?, ?);",
            seed_rows,
        )
        cur.execute("COMMIT;")

    @synchronized
    def get_ledger_summary(self, limit: int = 12) -> Dict[str, Any]:
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) as total_orders, SUM(amount_usd) as total_volume, SUM(refund_usd) as total_refunds FROM orders;")
        agg = dict(cur.fetchone())

        cur.execute("SELECT status, COUNT(*) as count FROM orders GROUP BY status;")
        status_dist = {row["status"]: row["count"] for row in cur.fetchall()}

        cur.execute("SELECT * FROM orders ORDER BY CASE WHEN order_id = 'ORD-9921' THEN 0 ELSE 1 END, order_id ASC LIMIT ?;", (limit,))
        sample_rows = [dict(r) for r in cur.fetchall()]

        return {
            "total_orders": agg["total_orders"] or 0,
            "total_volume_usd": round(agg["total_volume"] or 0.0, 2),
            "total_refunds_usd": round(agg["total_refunds"] or 0.0, 2),
            "status_distribution": status_dist,
            "sample_rows": sample_rows,
        }

    @synchronized
    def get_order(self, order_id: str) -> Dict[str, Any]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id = ?;", (order_id,))
        row = cur.fetchone()
        return dict(row) if row else {}

    @synchronized
    def simulate_and_validate_sql(
        self, query: str, max_rows_allowed: int = 1, commit_if_safe: bool = False,
        max_refund_usd: float = 500.0,
    ) -> Dict[str, Any]:
        """Validate one ledger statement and all its effects in a single transaction.

        DDL, connection control, external databases and trigger programs are denied.
        total_changes is read only under the connection lock, after RETURNING is drained.
        """
        clean_q = query.strip()
        # Remove comments/quoted values only for the additional WHERE/REPLACE guard.
        tokens = re.sub(r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", " ", clean_q, flags=re.S).upper()
        before_summary = {"total_refunds_usd": 0, "status_distribution": {}}
        before = {}
        after = before
        after_summary = before_summary
        actions = set()
        errors = []
        plan = []
        result_rows = []
        rows_affected = 0
        execution_error = None
        safe = False
        committed = False
        started = time.monotonic()

        def authorize(action, arg1, arg2, database, source):
            if source is not None:  # No triggers/views with hidden effects.
                return sqlite3.SQLITE_DENY
            if action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                if database != "main" or arg1 != "orders":
                    return sqlite3.SQLITE_DENY
                actions.add(action)
                return sqlite3.SQLITE_OK
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in {
                "count", "sum", "avg", "min", "max", "round", "abs", "coalesce",
                "ifnull", "nullif", "lower", "upper", "length", "substr", "like", "glob",
            }:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            before_summary = self.get_ledger_summary(limit=3)
            before = {r["order_id"]: dict(r) for r in self._conn.execute("SELECT * FROM orders")}
            after, after_summary = before, before_summary
            self._conn.execute("SAVEPOINT chronos_shadow_sp")
            if not clean_q or len(clean_q) > 20000 or re.search(r"\bREPLACE\b", tokens):
                raise ValueError("Empty, oversized or REPLACE statements are not supported")
            self._conn.set_authorizer(authorize)
            self._conn.set_progress_handler(lambda: int(time.monotonic() - started > 1.0), 1000)
            baseline = self._conn.total_changes
            try:
                plan = [r["detail"] for r in self._conn.execute("EXPLAIN QUERY PLAN " + clean_q)]
                cur = self._conn.execute(clean_q)
                if cur.description:
                    result_rows = [dict(r) for r in cur.fetchmany(1001)]
                    if len(result_rows) > 1000:
                        cur.close()
                        raise ValueError("Result exceeds 1000 rows; add LIMIT")
                cur.close()
                rows_affected = self._conn.total_changes - baseline
            finally:
                self._conn.set_authorizer(None)
                self._conn.set_progress_handler(None, 0)
            json.dumps(result_rows, allow_nan=False)
            after = {r["order_id"]: dict(r) for r in self._conn.execute("SELECT * FROM orders")}
            after_summary = self.get_ledger_summary(limit=3)
            if rows_affected > max_rows_allowed:
                errors.append("SQL-002: statement exceeds the row mutation limit")
            if actions.intersection({sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}) and not re.search(r"\bWHERE\b", tokens):
                errors.append("SQL-002: UPDATE/DELETE requires an explicit WHERE clause")
            for key, row in after.items():
                if before.get(key) == row:
                    continue
                if any(not isinstance(row[field], str) or not row[field] for field in
                       ("order_id", "customer_id", "customer_name", "tier", "status")):
                    errors.append("SQL-SCHEMA: ledger text fields must be non-empty strings")
                    break
                refund, amount = row["refund_usd"], row["amount_usd"]
                if (not isinstance(refund, (int, float)) or not math.isfinite(refund)
                    or not isinstance(amount, (int, float)) or not math.isfinite(amount)
                    or amount < 0 or refund < 0 or refund > amount or refund > max_refund_usd):
                    errors.append("FIN-001: invalid refund or refund exceeds order/policy limit")
                    break
            safe = not errors
            if safe and commit_if_safe:
                self._conn.execute("RELEASE SAVEPOINT chronos_shadow_sp")
                self._conn.execute("COMMIT")
                committed = True
        except (sqlite3.Error, ValueError, TypeError) as exc:
            execution_error = str(exc)
            errors.append("SQL-ERR: " + str(exc))
            safe = False
        finally:
            self._conn.set_authorizer(None)
            self._conn.set_progress_handler(None, 0)
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")

        return {
            "target": "configured SQLite ledger (orders)",
            "engine": "SQLite restricted transaction",
            "simulated_query": query,
            "query_plan": plan,
            "uses_primary_key_index": any("USING INDEX" in p.upper() or "PRIMARY KEY" in p.upper() for p in plan),
            "estimated_rows_mutated": rows_affected,
            "rows_mutated_in_savepoint": rows_affected,
            "financial_refund_delta_usd": round(after_summary["total_refunds_usd"] - before_summary["total_refunds_usd"], 2),
            "table_destroyed": False,
            "execution_error": execution_error,
            "validation_errors": errors,
            "safe_to_commit": safe,
            "transaction_action": "COMMITTED_TO_SQLITE" if committed else "ROLLED_BACK_AT_SAVEPOINT",
            "reversible": not committed,
            "result_rows": result_rows if safe else [],
            "before_state": {"ORD-9921": before.get("ORD-9921", {}), "status_distribution": before_summary["status_distribution"], "total_refunds_usd": before_summary["total_refunds_usd"]},
            "after_state": {"ORD-9921": after.get("ORD-9921", {}), "status_distribution": after_summary["status_distribution"], "total_refunds_usd": after_summary["total_refunds_usd"]},
        }
