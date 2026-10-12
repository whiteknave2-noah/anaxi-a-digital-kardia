"""
Anaxi Relational History -- experimental, deliberately separate from
Anaxi Core. See ANAXI_IMPLEMENTATION_CONTRACT.md, "Experimental
relational architecture" -- this is not a second constitutional
protocol, and it has no write access to active_kardia, kardia_history,
or proposals. Its only sanctioned connection to Anaxi Core is an
optional linked_proposal_id: a pointer for later reconstruction, never
a trigger for automatic behavior.

Smallest version, per the design this implements:
    A. relational_events table -- append-only at the API level: no
       update/delete method exists on this class. That is API
       immutability (no public mutation operation after insertion), and
       it's what this module actually guarantees. It is not storage
       immutability (nothing with access to the underlying SQLite file
       or this class's own .conn/.cursor could modify a row) -- that
       would need different infrastructure: append-only logs, restricted
       file permissions, or something like hash-chaining, none of which
       this implements. Worth being precise about which one is true here.
    B. Explicit, separate participant perspectives -- agent_observation
       and external_observation are never collapsed into one "what
       happened" field, and neither is treated as ground truth.
    C. Links into existing proposals -- a reference column, nothing more.

`substrate` is recorded on every event so relational history from
different pipelines is never silently conflated -- directly relevant
given this project's core caution about attributing effects to Anaxi
that actually belong to whichever model produced them.

CORRECTION, established directly against the real call sites rather
than assumed: `substrate` is a legacy PIPELINE discriminator (its only
values in use are "llama" and "claude", one per entry-point script/DB),
not exact model identity. As of the Gemma waking-substrate switch,
llama_anaxi.py's calls into this class still pass substrate="llama"
even though the underlying waking model is now gemma4:e4b -- `substrate`
itself was never migrated to a new value and still carries only the
legacy pipeline discriminator, on purpose (frozen spec, Amendment A1
§A1.8: canonical-before-legacy transaction ordering and historical
migration attribution rules are unchanged). Exact model provenance now
DOES have a place to live here: record_event()'s optional
model_revision_id/pipeline_id/epoch_id/auth_context_id/event_id
kwargs (identity/provenance schema, §3 additive delta), populated only
by a native waking turn's post-commit legacy projection, from values
already resolved from that turn's own committed canonical event --
never fabricated here, and every existing caller that never passes
them is unaffected.
"""

import json
import sqlite3
import time
from typing import Optional, Dict, Any, List


class RelationalHistory:
    def __init__(self, db_path: str = "anaxi_relational.db"):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self._init_table()

    def _init_table(self) -> None:
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS relational_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                substrate TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                agent_observation TEXT,
                external_observation TEXT,
                agent_response TEXT,
                linked_memories TEXT,
                linked_proposal_id INTEGER,
                status TEXT NOT NULL DEFAULT 'recorded'
            )
        """)
        # Identity/provenance §3 is additive. A legitimate database created
        # by an older release already has relational_events but lacks these
        # nullable columns; CREATE TABLE IF NOT EXISTS alone cannot upgrade
        # it, so ordinary restart must apply the bounded additive delta.
        columns = {
            row[1] for row in self.cursor.execute(
                "PRAGMA table_info(relational_events)"
            ).fetchall()
        }
        for name in (
            "model_revision_id", "pipeline_id", "epoch_id",
            "auth_context_id", "event_id",
        ):
            if name not in columns:
                self.cursor.execute(
                    f"ALTER TABLE relational_events ADD COLUMN {name} TEXT"
                )
        self.conn.commit()

    def record_event(
        self,
        user_id: str,
        substrate: str,
        agent_observation: Optional[str] = None,
        external_observation: Optional[str] = None,
        agent_response: Optional[str] = None,
        linked_memories: Optional[List[str]] = None,
        linked_proposal_id: Optional[int] = None,
        model_revision_id: Optional[str] = None,
        pipeline_id: Optional[str] = None,
        epoch_id: Optional[str] = None,
        auth_context_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> int:
        """Append-only. Record what actually happened -- none of these
        fields are mandatory beyond user_id/substrate, per the design's
        explicit instruction not to manufacture relational metadata
        because a schema expects it.

        NATIVE-PATH EDIT: model_revision_id/pipeline_id/
        epoch_id/auth_context_id/event_id are the complete §3 additive-
        delta field set for relational_events (frozen
        DESIGN_NOTE_identity_provenance_schema.md §3) -- all optional,
        all default None, so every existing (historical/Claude-pipeline)
        caller that never passes them is unaffected. Only a native
        waking turn's post-commit legacy projection populates them, and
        only with values already resolved from that turn's own
        committed canonical event -- never fabricated here."""
        now = int(time.time())
        self.cursor.execute(
            """INSERT INTO relational_events
               (user_id, substrate, created_at, agent_observation,
                external_observation, agent_response, linked_memories,
                linked_proposal_id, status,
                model_revision_id, pipeline_id, epoch_id, auth_context_id, event_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'recorded', ?, ?, ?, ?, ?)""",
            (user_id, substrate, now, agent_observation, external_observation,
             agent_response, json.dumps(linked_memories or []), linked_proposal_id,
             model_revision_id, pipeline_id, epoch_id, auth_context_id, event_id),
        )
        self.conn.commit()
        return self.cursor.lastrowid

    def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        self.cursor.execute("SELECT * FROM relational_events WHERE id = ?", (event_id,))
        row = self.cursor.fetchone()
        if not row:
            return None
        record = dict(zip((d[0] for d in self.cursor.description), row))
        record["linked_memories"] = json.loads(record["linked_memories"] or "[]")
        return record

    def events_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        self.cursor.execute(
            "SELECT id FROM relational_events WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        )
        return [self.get_event(r[0]) for r in self.cursor.fetchall()]

    def events_for_proposal(self, proposal_id: int) -> List[Dict[str, Any]]:
        """Reconstructs the sequence around a proposal. Deliberately
        returns only recorded events and a reference -- nothing here
        asserts that a linked event caused the proposal, only that it
        preceded and was linked to it."""
        self.cursor.execute(
            "SELECT id FROM relational_events WHERE linked_proposal_id = ? ORDER BY created_at",
            (proposal_id,),
        )
        return [self.get_event(r[0]) for r in self.cursor.fetchall()]

    def close(self) -> None:
        self.conn.close()
