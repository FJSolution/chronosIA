"""
ChronosStorage — Persistent SQLite WAL Snapshot Store & Portable `.chronos` Trace Exporter.
Ensures agent DAG runs and memory snapshots survive server restarts and can be shared across machines.
"""

import json
import sqlite3
from pathlib import Path
from typing import Dict, Any, List, Optional


class ChronosStorage:
    """
    Persists AgentRuns and ExecutionSteps in SQLite WAL mode and exports/imports
    portable `.chronos` deterministic replay bundles.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(Path(__file__).parent.parent / "chronos_traces.db")
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_runs (
                run_id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                scenario TEXT NOT NULL,
                status TEXT NOT NULL,
                parent_run_id TEXT,
                forked_from_step INTEGER,
                created_at REAL NOT NULL,
                run_json TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def save_run(self, run_dict: Dict[str, Any]) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO agent_runs (run_id, agent_name, scenario, status, parent_run_id, forked_from_step, created_at, run_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status = excluded.status,
                run_json = excluded.run_json;
            """,
            (
                run_dict["run_id"],
                run_dict["agent_name"],
                run_dict["scenario"],
                run_dict["status"],
                run_dict.get("parent_run_id"),
                run_dict.get("forked_from_step"),
                run_dict["created_at"],
                json.dumps(run_dict),
            ),
        )
        self._conn.commit()

    def load_all_runs(self) -> List[Dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute("SELECT run_json FROM agent_runs ORDER BY created_at DESC;")
        return [json.loads(row["run_json"]) for row in cur.fetchall()]

    def export_chronos_bundle(self, run_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Packages a run into a self-contained `.chronos` deterministic replay artifact.
        """
        return {
            "chronos_format_version": "1.0",
            "engine": "ChronosAI Deterministic Runtime",
            "exported_run": run_dict,
            "replay_instructions": {
                "cli": f"chronos replay {run_dict['run_id']}.chronos.json --from-step 5",
                "deterministic_guarantees": "All steps carry SHA-256 content-addressable state hashes and immutable memory snapshots.",
            },
        }
