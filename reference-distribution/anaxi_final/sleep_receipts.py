"""
Anaxi -- Sleep cycle receipts. An observability/audit layer around the
sleep cycle, not a constitutional mechanism and not a memory node --
it records what a cycle did. It has no path to change what a cycle
does: nothing in orchestration.py, anaxi_protocol_sqlite.py, or
anaxi_protocol.py reads from this store, so a receipt can influence
nothing downstream.

Append-only, same pattern as relational_history.py: no update or
delete method exists on this class, so a completed receipt can't be
revised by a later cycle or a later call. Own dedicated database, kept
separate from active memory, Kardia, and relational history for the
same reason those are kept separate from each other.
"""

import json
import sqlite3
import uuid
from typing import Optional, List, Dict, Any


class SleepReceiptStore:
    def __init__(self, db_path: str = "anaxi_sleep_receipts.db"):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self._init_table()

    def _init_table(self) -> None:
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS sleep_receipts (
                cycle_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                sleep_start_timestamp INTEGER NOT NULL,
                sleep_completion_timestamp INTEGER,
                cycle_end_timestamp INTEGER,
                sleep_success INTEGER NOT NULL,
                identity_success INTEGER NOT NULL,
                memories_considered INTEGER NOT NULL,
                memories_retained INTEGER NOT NULL,
                memories_discarded INTEGER NOT NULL,
                kardia_reflections_generated INTEGER NOT NULL,
                proposals_created INTEGER NOT NULL,
                proposals_pending INTEGER NOT NULL,
                errors TEXT NOT NULL
            )
        """)
        self.conn.commit()

    @staticmethod
    def new_cycle_id() -> str:
        return str(uuid.uuid4())

    def record_receipt(
        self,
        cycle_id: str,
        user_id: str,
        sleep_start_timestamp: int,
        sleep_success: bool,
        identity_success: bool,
        memories_considered: int,
        memories_retained: int,
        memories_discarded: int,
        kardia_reflections_generated: int,
        proposals_created: int,
        proposals_pending: int,
        errors: Optional[List[str]] = None,
        sleep_completion_timestamp: Optional[int] = None,
        cycle_end_timestamp: Optional[int] = None,
    ) -> None:
        """Append-only insert. No corresponding update method exists on
        this class -- a completed cycle's receipt cannot be revised by
        a later cycle or a later call."""
        self.cursor.execute(
            """
            INSERT INTO sleep_receipts (
                cycle_id, user_id, sleep_start_timestamp,
                sleep_completion_timestamp, cycle_end_timestamp,
                sleep_success, identity_success,
                memories_considered, memories_retained, memories_discarded,
                kardia_reflections_generated, proposals_created, proposals_pending,
                errors
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_id, user_id, sleep_start_timestamp,
                sleep_completion_timestamp, cycle_end_timestamp,
                int(sleep_success), int(identity_success),
                memories_considered, memories_retained, memories_discarded,
                kardia_reflections_generated, proposals_created, proposals_pending,
                json.dumps(errors or []),
            ),
        )
        self.conn.commit()

    def get_receipt(self, cycle_id: str) -> Optional[Dict[str, Any]]:
        self.cursor.execute("SELECT * FROM sleep_receipts WHERE cycle_id = ?", (cycle_id,))
        row = self.cursor.fetchone()
        if not row:
            return None
        record = dict(zip((d[0] for d in self.cursor.description), row))
        record["sleep_success"] = bool(record["sleep_success"])
        record["identity_success"] = bool(record["identity_success"])
        record["errors"] = json.loads(record["errors"])
        return record

    def receipts_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        self.cursor.execute(
            "SELECT cycle_id FROM sleep_receipts WHERE user_id = ? ORDER BY sleep_start_timestamp",
            (user_id,),
        )
        return [self.get_receipt(r[0]) for r in self.cursor.fetchall()]

    def close(self) -> None:
        self.conn.close()
