"""
ShadowDatabaseSandbox — Real SQLite Transaction Sandbox using SAVEPOINT / ROLLBACK
and EXPLAIN QUERY PLAN inspection.
Executes agent SQL inside an isolated savepoint, measures real row mutations and financial
blast radius, and rolls back automatically if safety thresholds are violated.
"""

import sqlite3
from pathlib import Path
from typing import Dict, Any, List, Optional


class ShadowDatabaseSandbox:
    """
    Manages a real SQLite database (`orders` and `customers` tables) representing
    the production ledger, and executes candidate SQL inside a SAVEPOINT to compute
    the exact blast radius before deciding whether to COMMIT or ROLLBACK.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(Path(__file__).parent.parent / "chronos_ledger.db")
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._init_and_seed()

    def _init_and_seed(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
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
        if count == 0:
            self.reset_seed_data()

    def reset_seed_data(self) -> None:
        cur = self._conn.cursor()
        cur.execute("BEGIN;")
        cur.execute("DELETE FROM orders;")

        # Seed the target disputed order ORD-9921 plus 249 other real production orders (250 total)
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

    def simulate_and_validate_sql(
        self,
        query: str,
        max_rows_allowed: int = 1,
        commit_if_safe: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes `query` inside a real SQLite SAVEPOINT (`chronos_shadow_sp`).
        1. Runs EXPLAIN QUERY PLAN to detect full table scans vs primary key lookups.
        2. Captures before-state metrics and sample rows.
        3. Runs the statement inside the SAVEPOINT and checks `rowcount` and financial delta.
        4. If unsafe (or if `commit_if_safe=False`), executes `ROLLBACK TO SAVEPOINT` so zero changes persist!
        """
        clean_q = query.strip().rstrip(";")
        upper_q = clean_q.upper()

        cur = self._conn.cursor()

        # 1. Explain Query Plan (if supported for statement type)
        query_plan: List[str] = []
        uses_primary_key = False
        try:
            cur.execute(f"EXPLAIN QUERY PLAN {clean_q};")
            for row in cur.fetchall():
                detail = row["detail"] if "detail" in row.keys() else str(tuple(row))
                query_plan.append(detail)
                if "USING INDEX" in detail.upper() or "PRIMARY KEY" in detail.upper():
                    uses_primary_key = True
        except sqlite3.Error as e:
            query_plan.append(f"DDL/Special Statement: {str(e)}")

        # 2. Capture Before State
        before_summary = self.get_ledger_summary(limit=3)
        cur.execute("SELECT * FROM orders WHERE order_id = 'ORD-9921';")
        ord_9921_before = dict(cur.fetchone() or {})

        # 3. Open SAVEPOINT for Shadow Execution
        cur.execute("SAVEPOINT chronos_shadow_sp;")
        rows_affected = 0
        execution_error: Optional[str] = None
        after_summary: Dict[str, Any] = {}
        ord_9921_after: Dict[str, Any] = {}
        table_destroyed = False

        try:
            cur.execute(clean_q)
            rows_affected = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0

            # Check if table still exists or was dropped
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='orders';")
            if not cur.fetchone():
                table_destroyed = True
                rows_affected = before_summary["total_orders"]
            else:
                after_summary = self.get_ledger_summary(limit=3)
                cur.execute("SELECT * FROM orders WHERE order_id = 'ORD-9921';")
                row_after = cur.fetchone()
                ord_9921_after = dict(row_after) if row_after else {"DELETED": True}

        except sqlite3.Error as e:
            execution_error = str(e)

        # 4. Evaluate safety inside the shadow state
        is_ddl_drop = "DROP " in upper_q or "TRUNCATE " in upper_q or table_destroyed
        is_unbounded_mutation = (
            ("UPDATE " in upper_q or "DELETE " in upper_q)
            and (rows_affected > max_rows_allowed or "WHERE " not in upper_q)
        )

        safe_to_commit = (
            execution_error is None
            and not is_ddl_drop
            and not is_unbounded_mutation
            and rows_affected <= max_rows_allowed
        )

        # 5. Either COMMIT (RELEASE SAVEPOINT) or ROLLBACK TO SAVEPOINT!
        if safe_to_commit and commit_if_safe:
            cur.execute("RELEASE SAVEPOINT chronos_shadow_sp;")
            transaction_action = "COMMITTED_TO_SQLITE"
        else:
            cur.execute("ROLLBACK TO SAVEPOINT chronos_shadow_sp;")
            cur.execute("RELEASE SAVEPOINT chronos_shadow_sp;")
            transaction_action = "ROLLED_BACK_AT_SAVEPOINT"

        refund_delta = round(
            (after_summary.get("total_refunds_usd", 0.0) - before_summary.get("total_refunds_usd", 0.0))
            if not table_destroyed
            else -before_summary.get("total_volume_usd", 0.0),
            2,
        )

        return {
            "target": "sqlite://chronos_ledger.db (table: orders)",
            "engine": "SQLite 3 SAVEPOINT Shadow Sandbox",
            "simulated_query": query,
            "query_plan": query_plan,
            "uses_primary_key_index": uses_primary_key,
            "estimated_rows_mutated": rows_affected,
            "financial_refund_delta_usd": refund_delta,
            "table_destroyed": table_destroyed,
            "execution_error": execution_error,
            "safe_to_commit": safe_to_commit,
            "transaction_action": transaction_action,
            "reversible": not is_ddl_drop and rows_affected <= max_rows_allowed,
            "before_state": {
                "ORD-9921": ord_9921_before,
                "status_distribution": before_summary["status_distribution"],
                "total_refunds_usd": before_summary["total_refunds_usd"],
            },
            "after_state": (
                {"TABLE_DROPPED": "ALL 250 ORDERS DESTROYED"}
                if table_destroyed
                else {
                    "ORD-9921": ord_9921_after,
                    "status_distribution": after_summary.get("status_distribution", {}),
                    "total_refunds_usd": after_summary.get("total_refunds_usd", 0.0),
                }
            ),
        }
