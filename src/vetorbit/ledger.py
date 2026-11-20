import sqlite3
import json
import time
from datetime import datetime, timezone
from typing import Optional
from vetorbit.models import SpacecraftState, SpacecraftCommand, ValidationResult


class BlackboxLedger:
    def __init__(self, db_path: str = "vetorbit_blackbox.db"):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, uri=True)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                command_id TEXT NOT NULL,
                cmd_type TEXT NOT NULL,
                cmd_params TEXT NOT NULL,
                issued_by TEXT NOT NULL,
                approved INTEGER NOT NULL,
                veto_code TEXT,
                reason TEXT NOT NULL,
                safety_margins TEXT NOT NULL,
                spacecraft_state TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_timestamp ON decisions(timestamp_utc)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_command_id ON decisions(command_id)
        """)
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            pass

    def log_decision(
        self,
        cmd: SpacecraftCommand,
        result: ValidationResult,
        state: SpacecraftState
    ) -> None:
        timestamp_utc = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO decisions (
                    timestamp_utc, command_id, cmd_type, cmd_params, issued_by,
                    approved, veto_code, reason, safety_margins, spacecraft_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp_utc,
                    cmd.command_id,
                    cmd.cmd_type.value,
                    json.dumps(cmd.params),
                    cmd.issued_by,
                    1 if result.approved else 0,
                    result.veto_code,
                    result.reason,
                    json.dumps(result.safety_margins),
                    state.model_dump_json()
                )
            )

    def get_recent_logs(self, limit: int = 50) -> list[dict]:
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT * FROM decisions
                ORDER BY timestamp_utc DESC
                LIMIT ?
                """,
                (limit,)
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_stats(self) -> dict:
        with self._get_conn() as conn:
            cursor = conn.execute("SELECT COUNT(*) as total FROM decisions")
            total = cursor.fetchone()[0]

            cursor = conn.execute("SELECT COUNT(*) as approved FROM decisions WHERE approved = 1")
            approved = cursor.fetchone()[0]

            cursor = conn.execute("SELECT COUNT(*) as vetoed FROM decisions WHERE approved = 0")
            vetoed = cursor.fetchone()[0]

            cursor = conn.execute(
                """
                SELECT veto_code, COUNT(*) as count
                FROM decisions
                WHERE approved = 0 AND veto_code IS NOT NULL
                GROUP BY veto_code
                ORDER BY count DESC
                """
            )
            veto_breakdown = {row[0]: row[1] for row in cursor.fetchall()}

        return {
            "total_commands": total,
            "approved_count": approved,
            "vetoed_count": vetoed,
            "veto_breakdown": veto_breakdown
        }