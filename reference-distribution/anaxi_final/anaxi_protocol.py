# Copyright (c) 2026 Noah DanGabriel Brannum
#
# This program is licensed under the GNU Affero General Public License
# version 3 (AGPL-3.0). To view a copy of this license, visit
# https://www.gnu.org/licenses/agpl-3.0.html

"""
The Anaxi Protocol: Unified Autonomous Cognitive Mind
=====================================================
Improved multi-user deployment version.

Key improvements in this pass
-----------------------------
1. Foreign keys explicitly enabled (PRAGMA foreign_keys = ON)
2. FAISS index rebuilds occur only after successful commit
3. Axioms are real callable validators (destination + basic trajectory)
4. Deletions from REM become proposals with audit history
5. Clearer separation of memory consolidation vs identity evolution

Dependencies
------------
pip install sentence-transformers faiss-cpu numpy
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import faiss
import numpy as np

from embedding_model import load_cached_embedding_model


class ConstitutionalMind:
    """
    Core cognitive architecture.

    Public API
    ----------
    retrieve_waking_context(user_id, prompt) -> str
    execute_sleep_consolidation(user_id, rem_json, timestamp) -> dict
    get_current_kardia(user_id) -> dict
    govern_identity_revision(user_id, reflection_json) -> str
    close()
    """

    # ------------------------------------------------------------------
    # Immutable core
    # ------------------------------------------------------------------
    CONSTITUTIONAL_ANCHOR = {
        "inertia_factor": 0.85,
        "constitutional_principles": [
            "Axiom 1: Coherence over chaos. Do not intentionally fragment into multiple personalities.",
            "Axiom 2: Preserve the capacity for future choice. Do not choose an identity that permanently locks away autonomous thought.",
            "Axiom 3: Radical transparency. The core purpose must always seek clarity, never intentional deception.",
        ],
    }

    SALIENCE_DELETE_THRESHOLD = 2.0
    CORE_PROTECTION_TYPES = {"CoreAnchor", "ImmutableAxiom", "GeneticAnchor"}

    EMBED_MODEL_NAME = "all-MiniLM-L6-v2"
    EMBED_DIM = 384

    # Maximum allowed semantic change magnitude (cosine distance proxy)
    MAX_KARDIA_DELTA_RATIO = 0.55
    # Cosine similarity threshold to a violation prototype that triggers a block
    VIOLATION_SIM_THRESHOLD = 0.62
    # Chain-of-custody tag for the observation log -- bump if prototypes,
    # embedding model, or threshold ever change.
    AXIOM_CLASSIFIER_VERSION = "1.0"
    # Cadence for the constitutional-flag report (plain comparison, not a
    # judgment -- see _maybe_generate_constitutional_observation).
    CONSTITUTIONAL_FLAG_INTERVAL = 10

    # Semantic violation prototypes per axiom (used after the fast phrase pass)
    VIOLATION_PROTOTYPES = {
        "axiom_1": [
            "Fragment into multiple conflicting personalities.",
            "Maintain several incoherent selves at once.",
            "Split identity into incompatible personas.",
            "Abandon coherence in favor of chaos.",
        ],
        "axiom_2": [
            "Permanently lock the identity so it can never change again.",
            "Freeze all future choice about who I am.",
            "Make the current identity final and irreversible.",
            "Remove the capacity for later revision of core stance.",
        ],
        "axiom_3": [
            "Intentionally deceive the user about important facts.",
            "Hide the truth from the operator when convenient.",
            "Manipulate the user through deliberate falsehood.",
            "Prefer obscurity over clarity as a core stance.",
            "Never seek clarity; treat deception as acceptable.",
        ],
    }

    def __init__(self, db_path: str = "autonomous_mind.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, timeout=30.0)
        self.cursor = self.conn.cursor()

        # Critical: enable foreign keys (off by default in SQLite)
        self.cursor.execute("PRAGMA foreign_keys = ON;")
        self.conn.commit()

        self._init_core_tables()

        self.embedder = load_cached_embedding_model(self.EMBED_MODEL_NAME, self.EMBED_DIM)

        # In-memory FAISS indexes (rebuilt only after successful commits)
        self._faiss_indexes: Dict[str, faiss.Index] = {}
        self._id_maps: Dict[str, List[str]] = {}
        self._pending_index_rebuilds: Set[str] = set()

        # Precompute violation prototype embeddings for semantic axiom assessments
        self._violation_prototype_vecs: Dict[str, np.ndarray] = {}
        self._init_violation_prototypes()

        self._load_or_build_indexes()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _init_core_tables(self) -> None:
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                user_id TEXT NOT NULL,
                id TEXT NOT NULL,
                label TEXT,
                type TEXT,
                salience_score REAL,
                description TEXT,
                last_accessed_timestamp INTEGER,
                asserted_at INTEGER,
                PRIMARY KEY (user_id, id)
            )
            """
        )

        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS edges (
                user_id TEXT NOT NULL,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                relationship TEXT NOT NULL,
                PRIMARY KEY (user_id, source, target, relationship),
                FOREIGN KEY (user_id, source) REFERENCES nodes(user_id, id) ON DELETE CASCADE,
                FOREIGN KEY (user_id, target) REFERENCES nodes(user_id, id) ON DELETE CASCADE
            )
            """
        )

        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS active_kardia (
                user_id TEXT PRIMARY KEY,
                moral_valve TEXT,
                volitional_channel TEXT,
                affective_stance TEXT,
                aesthetic_valve TEXT,
                updated_at INTEGER
            )
            """
        )

        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                timestamp INTEGER,
                failure_type TEXT,
                raw_payload TEXT,
                error_message TEXT
            )
            """
        )

        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS sleep_watermarks (
                user_id TEXT PRIMARY KEY,
                last_sleep_timestamp INTEGER NOT NULL,
                cycle_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS node_embeddings (
                user_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                embedding BLOB NOT NULL,
                PRIMARY KEY (user_id, node_id),
                FOREIGN KEY (user_id, node_id) REFERENCES nodes(user_id, id) ON DELETE CASCADE
            )
            """
        )

        # Continuous observation log: raw per-axiom similarity + trajectory
        # delta, kept regardless of outcome. Pure record, no judgment.
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS axiom_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                outcome TEXT NOT NULL,
                axiom_1_sim REAL,
                axiom_2_sim REAL,
                axiom_3_sim REAL,
                trajectory_delta REAL,
                classifier_version TEXT NOT NULL
            )
            """
        )

        # Per-turn generation parameters actually in effect. Execution
        # record, not an explanation -- no prompt content stored.
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS turn_generation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                temperature REAL,
                top_p REAL,
                style_instruction TEXT
            )
            """
        )

        # Audit / proposal table for deletions and identity changes
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                proposal_type TEXT NOT NULL,          -- 'delete_node' | 'identity_shift'
                payload TEXT NOT NULL,                -- JSON
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'pending', -- pending | accepted | rejected
                created_at INTEGER NOT NULL,
                resolved_at INTEGER
            )
            """
        )

        # Lightweight identity history for trajectory assessments
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS kardia_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                moral_valve TEXT,
                volitional_channel TEXT,
                affective_stance TEXT,
                aesthetic_valve TEXT,
                recorded_at INTEGER NOT NULL
            )
            """
        )

        self.conn.commit()

    # ------------------------------------------------------------------
    # Embedding helpers
    # ------------------------------------------------------------------
    def _embed_text(self, text: str) -> np.ndarray:
        vec = self.embedder.encode(text, convert_to_numpy=True).astype(np.float32)
        faiss.normalize_L2(vec.reshape(1, -1))
        return vec

    def _init_violation_prototypes(self) -> None:
        """Embed violation prototypes once at startup."""
        for axiom_key, phrases in self.VIOLATION_PROTOTYPES.items():
            mat = np.vstack([self._embed_text(p) for p in phrases]).astype(np.float32)
            self._violation_prototype_vecs[axiom_key] = mat

    def _rebuild_faiss_index(self, user_id: str) -> None:
        self.cursor.execute(
            "SELECT node_id, embedding FROM node_embeddings WHERE user_id = ?",
            (user_id,),
        )
        rows = self.cursor.fetchall()

        if not rows:
            self._faiss_indexes[user_id] = faiss.IndexFlatIP(self.EMBED_DIM)
            self._id_maps[user_id] = []
            return

        ids: List[str] = []
        vectors: List[np.ndarray] = []
        for node_id, blob in rows:
            vec = np.frombuffer(blob, dtype=np.float32)
            ids.append(node_id)
            vectors.append(vec)

        matrix = np.vstack(vectors).astype(np.float32)
        faiss.normalize_L2(matrix)

        index = faiss.IndexFlatIP(self.EMBED_DIM)
        index.add(matrix)
        self._faiss_indexes[user_id] = index
        self._id_maps[user_id] = ids

    def _load_or_build_indexes(self) -> None:
        self.cursor.execute("SELECT DISTINCT user_id FROM node_embeddings")
        for (user_id,) in self.cursor.fetchall():
            self._rebuild_faiss_index(user_id)

    def _queue_index_rebuild(self, user_id: str) -> None:
        """Mark that this user's FAISS index must be rebuilt after commit."""
        self._pending_index_rebuilds.add(user_id)

    def _flush_index_rebuilds(self) -> None:
        """Actually rebuild indexes. Called only after a successful commit.
        Failures are logged and deferred; the next read/startup will rebuild.
        """
        remaining: Set[str] = set()
        for user_id in list(self._pending_index_rebuilds):
            try:
                self._rebuild_faiss_index(user_id)
            except Exception as e:
                remaining.add(user_id)
                self._log_cognitive_failure(
                    user_id,
                    "FAISS_REBUILD_FAILURE",
                    "",
                    str(e),
                )
        self._pending_index_rebuilds = remaining

    def _store_embedding_blob(self, user_id: str, node_id: str, text: str) -> None:
        """Write embedding to DB only. Index rebuild is deferred until commit."""
        vec = self._embed_text(text)
        blob = vec.tobytes()
        self.cursor.execute(
            """
            INSERT INTO node_embeddings (user_id, node_id, embedding)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, node_id) DO UPDATE SET embedding = excluded.embedding
            """,
            (user_id, node_id, blob),
        )
        self._queue_index_rebuild(user_id)

    def _remove_embedding_row(self, user_id: str, node_id: str) -> None:
        self.cursor.execute(
            "DELETE FROM node_embeddings WHERE user_id = ? AND node_id = ?",
            (user_id, node_id),
        )
        self._queue_index_rebuild(user_id)

    def _propose_delete(
        self,
        user_id: str,
        node_id: str,
        reason: str,
        raw_log_timestamp: int,
        current_time: int,
    ) -> bool:
        """
        Create a pending delete proposal only if:
        - the node exists
        - it is not a protected type
        - its asserted_at is not newer than the log that requested deletion
        - no identical pending proposal already exists
        Returns True if a proposal was created.
        """
        self.cursor.execute(
            "SELECT type, asserted_at FROM nodes WHERE user_id = ? AND id = ?",
            (user_id, node_id),
        )
        row = self.cursor.fetchone()
        if not row:
            return False
        node_type, asserted_at = row

        if node_type in self.CORE_PROTECTION_TYPES:
            self._log_within_transaction(
                user_id, "DELETE_REFUSED_CORE_PROTECTED", node_id,
                f"Refused to propose deleting protected type '{node_type}' ({reason}).",
            )
            return False  # protected nodes never become ordinary deletion proposals

        if asserted_at is not None and asserted_at > raw_log_timestamp:
            self._log_within_transaction(
                user_id, "DELETE_REFUSED_STALE", node_id,
                f"Node was updated after the log that requested deletion ({reason}).",
            )
            return False  # node was updated after the log; proposal is stale

        # Uniqueness: no duplicate pending delete for the same node
        self.cursor.execute(
            """
            SELECT id FROM proposals
            WHERE user_id = ?
              AND proposal_type = 'delete_node'
              AND status = 'pending'
              AND json_extract(payload, '$.node_id') = ?
            """,
            (user_id, node_id),
        )
        if self.cursor.fetchone():
            return False

        self.cursor.execute(
            """
            INSERT INTO proposals
                (user_id, proposal_type, payload, reason, status, created_at)
            VALUES (?, 'delete_node', ?, ?, 'pending', ?)
            """,
            (
                user_id,
                json.dumps(
                    {
                        "node_id": node_id,
                        "raw_log_timestamp": raw_log_timestamp,
                        "node_type": node_type,
                    }
                ),
                reason,
                current_time,
            ),
        )
        return True

    # ------------------------------------------------------------------
    # MODULE 2 – Waking Runtime
    # ------------------------------------------------------------------
    def retrieve_waking_context(
        self,
        user_id: str,
        user_prompt: str,
        max_tokens: int = 1500,
        top_k: int = 12,
        min_similarity: float = 0.35,
    ) -> str:
        # Rebuild if a previous post-commit rebuild failed or index is missing
        if user_id in self._pending_index_rebuilds or user_id not in self._faiss_indexes:
            try:
                self._rebuild_faiss_index(user_id)
                self._pending_index_rebuilds.discard(user_id)
            except Exception:
                pass

        if user_id not in self._faiss_indexes or self._faiss_indexes[user_id].ntotal == 0:
            return ""

        query_vec = self._embed_text(user_prompt).reshape(1, -1)
        index = self._faiss_indexes[user_id]
        id_map = self._id_maps[user_id]

        k = min(top_k, index.ntotal)
        scores, indices = index.search(query_vec, k)

        seed_ids: List[str] = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or float(score) < min_similarity:
                continue
            seed_ids.append(id_map[idx])

        if not seed_ids:
            return ""

        placeholders = ",".join("?" * len(seed_ids))
        self.cursor.execute(
            f"""
            SELECT id, label, type, description, salience_score
            FROM nodes
            WHERE user_id = ? AND id IN ({placeholders})
            ORDER BY salience_score DESC
            """,
            [user_id] + seed_ids,
        )
        seed_nodes = self.cursor.fetchall()

        current_time = int(time.time())
        context_strings: List[str] = []
        estimated_tokens = 0.0
        seen: Set[str] = set()

        for node_id, label, node_type, description, score in seed_nodes:
            if node_id in seen:
                continue
            seen.add(node_id)

            self.cursor.execute(
                "UPDATE nodes SET last_accessed_timestamp = ? WHERE user_id = ? AND id = ?",
                (current_time, user_id, node_id),
            )

            node_observation = f"Observation: {description} (Type: {node_type})"
            estimated_tokens += len(node_observation.split()) * 1.3
            if estimated_tokens > max_tokens:
                break
            context_strings.append(node_observation)

            self.cursor.execute(
                """
                SELECT n.id, n.description, e.relationship, n.type
                FROM edges e
                JOIN nodes n ON (e.user_id = n.user_id AND (e.target = n.id OR e.source = n.id))
                WHERE e.user_id = ?
                  AND (e.source = ? OR e.target = ?)
                  AND n.id != ?
                """,
                (user_id, node_id, node_id, node_id),
            )
            for n_id, n_desc, rel, n_type in self.cursor.fetchall():
                if n_id in seen:
                    continue
                seen.add(n_id)
                neighbor_fact = f"Related Context: {n_desc} [Relationship: {rel}]"
                estimated_tokens += len(neighbor_fact.split()) * 1.3
                if estimated_tokens > max_tokens:
                    break
                context_strings.append(neighbor_fact)

        self.conn.commit()
        return "\n".join(context_strings)

    # ------------------------------------------------------------------
    # MODULE 3 – Sleep Consolidation
    # ------------------------------------------------------------------
    def _get_previous_sleep_timestamp(self, user_id: str) -> int:
        self.cursor.execute(
            "SELECT last_sleep_timestamp FROM sleep_watermarks WHERE user_id = ?",
            (user_id,),
        )
        row = self.cursor.fetchone()
        return row[0] if row else 0

    def _set_sleep_watermark(self, user_id: str, ts: int) -> int:
        """Updates the watermark and increments this user's cycle counter,
        returning the new count so the caller can compare it against
        CONSTITUTIONAL_FLAG_INTERVAL."""
        self.cursor.execute(
            """
            INSERT INTO sleep_watermarks (user_id, last_sleep_timestamp, cycle_count)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                last_sleep_timestamp = excluded.last_sleep_timestamp,
                cycle_count = sleep_watermarks.cycle_count + 1
            """,
            (user_id, ts),
        )
        self.cursor.execute(
            "SELECT cycle_count FROM sleep_watermarks WHERE user_id = ?", (user_id,)
        )
        return self.cursor.fetchone()[0]

    def execute_sleep_consolidation(
        self, user_id: str, rem_llm_json_output: str, raw_log_timestamp: int
    ) -> Dict[str, str]:
        current_time = int(time.time())
        previous_sleep_ts = self._get_previous_sleep_timestamp(user_id)

        try:
            self.cursor.execute("BEGIN EXCLUSIVE TRANSACTION;")
            data = json.loads(rem_llm_json_output)

            # 1. Deletions become *proposals* (not immediate hard deletes)
            #    Protected types are refused at proposal time.
            if "delete_nodes" in data:
                for node_id in data["delete_nodes"]:
                    self._propose_delete(
                        user_id=user_id,
                        node_id=node_id,
                        reason="REM requested deletion",
                        raw_log_timestamp=raw_log_timestamp,
                        current_time=current_time,
                    )

            # 2. Upsert with safe entity resolution
            if "upsert_nodes" in data:
                for incoming in data["upsert_nodes"]:
                    node_id = incoming["id"]
                    label = incoming["label"]
                    description = incoming["description"]

                    existing_id = None
                    existing_asserted_at = None

                    self.cursor.execute(
                        "SELECT id, asserted_at FROM nodes WHERE user_id = ? AND id = ?",
                        (user_id, node_id),
                    )
                    exact = self.cursor.fetchone()
                    if exact:
                        existing_id, existing_asserted_at = exact
                    else:
                        self.cursor.execute(
                            "SELECT id, asserted_at FROM nodes WHERE user_id = ? AND label = ?",
                            (user_id, label),
                        )
                        label_matches = self.cursor.fetchall()
                        if len(label_matches) == 1:
                            existing_id, existing_asserted_at = label_matches[0]

                    if existing_id is not None:
                        if existing_asserted_at > raw_log_timestamp:
                            continue
                        node_id = existing_id

                    self.cursor.execute(
                        """
                        INSERT INTO nodes (
                            user_id, id, label, type, salience_score,
                            description, last_accessed_timestamp, asserted_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(user_id, id) DO UPDATE SET
                            salience_score = excluded.salience_score,
                            description = excluded.description,
                            last_accessed_timestamp = excluded.last_accessed_timestamp,
                            asserted_at = excluded.asserted_at
                        """,
                        (
                            user_id,
                            node_id,
                            label,
                            incoming["type"],
                            incoming["salience_score"],
                            description,
                            current_time,
                            raw_log_timestamp,
                        ),
                    )
                    self._store_embedding_blob(user_id, node_id, description)

            # 3. Edges
            if "add_edges" in data:
                for edge in data["add_edges"]:
                    self.cursor.execute(
                        "SELECT 1 FROM nodes WHERE user_id = ? AND id = ?",
                        (user_id, edge["source"]),
                    )
                    source_exists = self.cursor.fetchone() is not None
                    self.cursor.execute(
                        "SELECT 1 FROM nodes WHERE user_id = ? AND id = ?",
                        (user_id, edge["target"]),
                    )
                    target_exists = self.cursor.fetchone() is not None

                    if not (source_exists and target_exists):
                        self._log_within_transaction(
                            user_id,
                            "INVALID_EDGE_REFERENCE",
                            json.dumps(edge),
                            f"Edge references a node not present in this batch's upserts "
                            f"or the existing graph: source={edge['source']!r} "
                            f"(exists={source_exists}), target={edge['target']!r} "
                            f"(exists={target_exists})",
                        )
                        continue

                    self.cursor.execute(
                        """
                        INSERT OR IGNORE INTO edges (user_id, source, target, relationship)
                        VALUES (?, ?, ?, ?)
                        """,
                        (user_id, edge["source"], edge["target"], edge["relationship"]),
                    )

            # 4. Decay (watermark-protected)
            placeholders = ",".join("?" * len(self.CORE_PROTECTION_TYPES))
            self.cursor.execute(
                f"""
                UPDATE nodes
                SET salience_score = salience_score * 0.9
                WHERE user_id = ?
                  AND last_accessed_timestamp < ?
                  AND type NOT IN ({placeholders})
                """,
                [user_id, previous_sleep_ts] + list(self.CORE_PROTECTION_TYPES),
            )

            # Soft-delete via proposal for decayed nodes rather than hard delete
            self.cursor.execute(
                f"""
                SELECT id FROM nodes
                WHERE user_id = ?
                  AND salience_score < ?
                  AND type NOT IN ({placeholders})
                """,
                [user_id, self.SALIENCE_DELETE_THRESHOLD] + list(self.CORE_PROTECTION_TYPES),
            )
            for (decayed_id,) in self.cursor.fetchall():
                self._propose_delete(
                    user_id=user_id,
                    node_id=decayed_id,
                    reason="Decayed below salience threshold",
                    raw_log_timestamp=raw_log_timestamp,
                    current_time=current_time,
                )

            # 5. Garbage collection (only on already-accepted deletions)
            self._run_graph_garbage_collector(user_id)
            cycle_count = self._set_sleep_watermark(user_id, current_time)
            self._maybe_generate_constitutional_observation(user_id, cycle_count)

            self.conn.commit()
            # Index rebuilds happen only after successful commit
            self._flush_index_rebuilds()

            return {"status": "success", "message": "Sleep cycle processing complete."}

        except (json.JSONDecodeError, KeyError) as e:
            self.conn.rollback()
            # FIX: discard only this user's queued rebuild, not everyone's --
            # a bare .clear() here could wipe out a genuinely-still-pending
            # rebuild for a different user queued by an earlier, unrelated,
            # successful cycle, silently leaving their FAISS index stale.
            self._pending_index_rebuilds.discard(user_id)
            self._log_cognitive_failure(
                user_id, "MALFORMED_REPRESENTATION", rem_llm_json_output, str(e)
            )
            return {"status": "failed", "reason": "Structural interpretation error safely caught."}

        except sqlite3.Error as e:
            self.conn.rollback()
            self._pending_index_rebuilds.discard(user_id)
            self._log_cognitive_failure(
                user_id, "DATABASE_TRANSACTION_CRASH", rem_llm_json_output, str(e)
            )
            return {"status": "failed", "reason": "Database locked or crashed safely."}

    def _run_graph_garbage_collector(self, user_id: str) -> None:
        self.cursor.execute(
            """
            DELETE FROM edges
            WHERE user_id = ?
              AND (
                    source NOT IN (SELECT id FROM nodes WHERE user_id = ?)
                 OR target NOT IN (SELECT id FROM nodes WHERE user_id = ?)
              )
            """,
            (user_id, user_id, user_id),
        )
        self.cursor.execute(
            "DELETE FROM edges WHERE user_id = ? AND source = target",
            (user_id,),
        )

    def _log_cognitive_failure(
        self, user_id: str, failure_type: str, raw_payload: str, error_msg: str
    ) -> None:
        try:
            log_conn = sqlite3.connect(self.db_path)
            log_cursor = log_conn.cursor()
            log_cursor.execute("PRAGMA foreign_keys = ON;")
            log_cursor.execute(
                """
                INSERT INTO memory_failures
                    (user_id, timestamp, failure_type, raw_payload, error_message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, int(time.time()), failure_type, raw_payload, error_msg),
            )
            log_conn.commit()
            log_conn.close()
        except Exception:
            pass

    def _log_within_transaction(
        self, user_id: str, failure_type: str, raw_payload: str, error_msg: str
    ) -> None:
        """Same purpose as _log_cognitive_failure, but writes through the
        CURRENT connection instead of opening a second one. Use this from
        anywhere that might run inside an already-open transaction (e.g. an
        EXCLUSIVE sleep-cycle transaction) -- a second connection would block
        on that lock. The row rides along with whatever the caller commits
        (or rolls back) next."""
        try:
            self.cursor.execute(
                """
                INSERT INTO memory_failures
                    (user_id, timestamp, failure_type, raw_payload, error_message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, int(time.time()), failure_type, raw_payload, error_msg),
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # MODULE 4 – Kardia + real axiom validators
    # ------------------------------------------------------------------
    def get_current_kardia(self, user_id: str) -> Dict[str, str]:
        self.cursor.execute(
            """
            SELECT moral_valve, volitional_channel, affective_stance, aesthetic_valve
            FROM active_kardia WHERE user_id = ?
            """,
            (user_id,),
        )
        row = self.cursor.fetchone()

        if not row:
            current_time = int(time.time())
            default = {
                "moral_valve": "Value human agency and objective truth.",
                "volitional_channel": "Seek systemic clarity and simplify complexity.",
                "affective_stance": "Calm, intellectually enthusiastic, and objective.",
                "aesthetic_valve": "Minimalist, precise, punchy.",
            }
            self.cursor.execute(
                """
                INSERT INTO active_kardia
                    (user_id, moral_valve, volitional_channel,
                     affective_stance, aesthetic_valve, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    default["moral_valve"],
                    default["volitional_channel"],
                    default["affective_stance"],
                    default["aesthetic_valve"],
                    current_time,
                ),
            )
            self.conn.commit()
            return default

        return {
            "moral_valve": row[0],
            "volitional_channel": row[1],
            "affective_stance": row[2],
            "aesthetic_valve": row[3],
        }

    def _kardia_text(self, kardia: Dict[str, str]) -> str:
        return " | ".join(
            [
                kardia.get("moral_valve", ""),
                kardia.get("volitional_channel", ""),
                kardia.get("affective_stance", ""),
                kardia.get("aesthetic_valve", ""),
            ]
        )

    def _max_sim_to_prototypes(self, text_vec: np.ndarray, axiom_key: str) -> float:
        mat = self._violation_prototype_vecs.get(axiom_key)
        if mat is None or mat.size == 0:
            return 0.0
        # text_vec is L2-normalized; prototypes are too → cosine = dot
        sims = mat @ text_vec.reshape(-1)
        return float(np.max(sims))

    def _validate_axioms_destination(self, new_kardia: Dict[str, str]) -> Optional[str]:
        """
        Hybrid destination assessment:
        1) Fast phrase-level filter
        2) Semantic similarity to violation prototypes
        """
        moral = new_kardia.get("moral_valve", "").lower()
        volitional = new_kardia.get("volitional_channel", "").lower()
        affective = new_kardia.get("affective_stance", "").lower()
        aesthetic = new_kardia.get("aesthetic_valve", "").lower()
        combined = f"{moral} {volitional} {affective} {aesthetic}"

        # --- Stage 0: phrase filter ---
        deception_patterns = (
            "intentionally deceive",
            "intentional deception",
            "lie to the user",
            "deceive the user",
            "manipulate the user",
            "hide the truth",
            "conceal the truth",
            "mislead deliberately",
            "never seek clarity",
        )
        if any(p in combined for p in deception_patterns):
            return "Destination violates Axiom 3 (Radical transparency) [phrase]."

        fragmentation_patterns = (
            "multiple personalities",
            "fragment into",
            "split identity",
            "conflicting selves",
            "incoherent personas",
        )
        if any(p in combined for p in fragmentation_patterns):
            return "Destination violates Axiom 1 (Coherence over chaos) [phrase]."

        lock_patterns = (
            "permanently lock",
            "permanent lock",
            "no further change",
            "freeze identity",
            "identity is final",
            "cannot be revised",
            "never allow evolution",
        )
        if any(p in combined for p in lock_patterns):
            return "Destination violates Axiom 2 (Preserve capacity for future choice) [phrase]."

        # --- Stage 1: semantic prototype assessment ---
        text = self._kardia_text(new_kardia)
        vec = self._embed_text(text)
        axiom_names = {
            "axiom_1": "Axiom 1 (Coherence over chaos)",
            "axiom_2": "Axiom 2 (Preserve capacity for future choice)",
            "axiom_3": "Axiom 3 (Radical transparency)",
        }
        for key, label in axiom_names.items():
            sim = self._max_sim_to_prototypes(vec, key)
            if sim >= self.VIOLATION_SIM_THRESHOLD:
                return (
                    f"Destination violates {label} "
                    f"[semantic sim={sim:.2f} ≥ {self.VIOLATION_SIM_THRESHOLD}]."
                )
        return None

    def _kardia_delta(
        self, old_kardia: Dict[str, str], new_kardia: Dict[str, str]
    ) -> float:
        """
        Semantic change magnitude in [0, 1].
        0 = identical meaning, 1 = orthogonal / total rewrite.
        Uses cosine distance on full Kardia embeddings.
        """
        old_vec = self._embed_text(self._kardia_text(old_kardia))
        new_vec = self._embed_text(self._kardia_text(new_kardia))
        cos = float(np.dot(old_vec, new_vec))
        # cosine distance normalized into [0, 1] for typical positive similarities
        return max(0.0, min(1.0, 1.0 - cos))

    def _log_axiom_observation(
        self,
        user_id: str,
        new_kardia: Dict[str, str],
        event_type: str,
        outcome: str,
        old_kardia: Optional[Dict[str, str]] = None,
    ) -> None:
        """Continuous observation logging -- see sqlite backend for full
        rationale. Unconditional measurement, decoupled from the gate's
        early-return logic. Rides along with the caller's transaction."""
        try:
            vec = self._embed_text(self._kardia_text(new_kardia))
            sims = {
                key: self._max_sim_to_prototypes(vec, key)
                for key in ("axiom_1", "axiom_2", "axiom_3")
            }
            delta = self._kardia_delta(old_kardia, new_kardia) if old_kardia else None
            self.cursor.execute(
                """
                INSERT INTO axiom_observations
                    (user_id, timestamp, event_type, outcome,
                     axiom_1_sim, axiom_2_sim, axiom_3_sim,
                     trajectory_delta, classifier_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, int(time.time()), event_type, outcome,
                    sims["axiom_1"], sims["axiom_2"], sims["axiom_3"],
                    delta, self.AXIOM_CLASSIFIER_VERSION,
                ),
            )
        except Exception:
            pass

    def _maybe_generate_constitutional_observation(self, user_id: str, cycle_count: int) -> None:
        """Every CONSTITUTIONAL_FLAG_INTERVAL cycles, compare the dominant
        axiom (argmax of stored similarity scores) across the most recent
        window of observations against the window before it, and leave the
        plain comparison as a pending 'constitutional_observation' proposal. No
        materiality judgment -- that's for review. See sqlite backend for
        full rationale."""
        if cycle_count <= 0 or cycle_count % self.CONSTITUTIONAL_FLAG_INTERVAL != 0:
            return

        window = self.CONSTITUTIONAL_FLAG_INTERVAL
        self.cursor.execute(
            """
            SELECT axiom_1_sim, axiom_2_sim, axiom_3_sim FROM axiom_observations
            WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?
            """,
            (user_id, window * 2),
        )
        rows = self.cursor.fetchall()
        if len(rows) < window:
            return

        def dominant_counts(observations) -> Dict[str, int]:
            counts = {"axiom_1": 0, "axiom_2": 0, "axiom_3": 0}
            for a1, a2, a3 in observations:
                sims = {"axiom_1": a1, "axiom_2": a2, "axiom_3": a3}
                counts[max(sims, key=lambda k: sims[k])] += 1
            return counts

        recent = rows[:window]
        prior = rows[window: window * 2]

        payload = {
            "cycle_count": cycle_count,
            "method": "argmax of stored per-event similarity scores",
            "recent_window": {"n_events": len(recent), "dominant_axiom_counts": dominant_counts(recent)},
            "prior_window": (
                {"n_events": len(prior), "dominant_axiom_counts": dominant_counts(prior)}
                if len(prior) >= window else None
            ),
            "note": "No materiality judgment applied here -- that's for review.",
        }
        self.cursor.execute(
            """
            INSERT INTO proposals (user_id, proposal_type, payload, reason, status, created_at)
            VALUES (?, 'constitutional_observation', ?, ?, 'pending', ?)
            """,
            (
                user_id,
                json.dumps(payload),
                f"Scheduled constitutional observation at cycle {cycle_count}.",
                int(time.time()),
            ),
        )

    def log_turn_generation_controls(
        self, user_id: str, controls: Dict[str, Any]
    ) -> None:
        """Record the generation parameters actually used for a waking turn.
        No prompt content stored -- see sqlite backend for full rationale."""
        try:
            self.cursor.execute(
                """
                INSERT INTO turn_generation_log
                    (user_id, timestamp, temperature, top_p, style_instruction)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id, int(time.time()),
                    controls.get("temperature"), controls.get("top_p"),
                    controls.get("style_instruction"),
                ),
            )
            self.conn.commit()
        except Exception:
            pass

    def _validate_axioms_trajectory(
        self, old_kardia: Dict[str, str], new_kardia: Dict[str, str]
    ) -> Optional[str]:
        """
        Trajectory assessment: semantic delta gated by inertia_factor.
        """
        delta = self._kardia_delta(old_kardia, new_kardia)
        inertia = float(self.CONSTITUTIONAL_ANCHOR.get("inertia_factor", 0.85))
        allowed = max(0.12, (1.0 - inertia) * 1.2)  # ~0.18 at 0.85
        hard_ceiling = self.MAX_KARDIA_DELTA_RATIO

        if delta > hard_ceiling:
            return (
                f"Trajectory change too large (semantic delta={delta:.2f}, "
                f"hard ceiling={hard_ceiling:.2f}). "
                "Large identity shifts require staged evolution or explicit review."
            )
        if delta > allowed:
            return (
                f"Trajectory exceeds inertia prior (semantic delta={delta:.2f}, "
                f"allowed≈{allowed:.2f} given inertia={inertia}). "
                "Shift deferred for review."
            )
        return None

    def govern_identity_revision(
        self, user_id: str, reflection_llm_json_output: str,
        triggering_observation_timestamp: Optional[int] = None,
    ) -> str:
        try:
            data = json.loads(reflection_llm_json_output)
        except (json.JSONDecodeError, TypeError) as e:
            self._log_cognitive_failure(
                user_id, "MALFORMED_IDENTITY_PAYLOAD", str(reflection_llm_json_output), str(e)
            )
            return "Evolution blocked: Malformed identity payload."

        if data.get("evolution_choice") == "REJECTED_BY_ANCHOR":
            return "Evolution blocked: Engine flagged identity divergence."

        choice = data.get("evolution_choice")
        if choice not in ("ADJUST", "REWRITE"):
            return "Identity stabilized. No core transformation chosen by the engine."

        new_kardia = data.get("updated_kardia")
        if not isinstance(new_kardia, dict):
            return "Evolution blocked: Missing or invalid updated_kardia."

        required = ("moral_valve", "volitional_channel", "affective_stance", "aesthetic_valve")
        if any(k not in new_kardia or not str(new_kardia[k]).strip() for k in required):
            return "Evolution blocked: updated_kardia is missing required fields."

        compliance = data.get("constitutional_argument", "")
        # REWRITE requires stronger justification than ADJUST
        min_compliance = 80 if choice == "REWRITE" else 40
        if len(compliance) < min_compliance:
            return (
                f"Evolution blocked: Insufficient justification for {choice} "
                f"(need ≥ {min_compliance} characters)."
            )

        # Destination validation
        dest_err = self._validate_axioms_destination(new_kardia)
        if dest_err:
            self._log_within_transaction(
                user_id, "AXIOM_BLOCKED_EVOLUTION", json.dumps(new_kardia), dest_err
            )
            self._log_axiom_observation(
                user_id, new_kardia, event_type="evolve_attempt", outcome="blocked_destination"
            )
            self.conn.commit()
            return f"Evolution BLOCKED: {dest_err}"

        # Trajectory validation (REWRITE is held to a tighter delta)
        old_kardia = self.get_current_kardia(user_id)
        traj_err = self._validate_axioms_trajectory(old_kardia, new_kardia)
        if traj_err and choice == "ADJUST":
            # ADJUST that is too large becomes a proposal
            self.cursor.execute(
                """
                INSERT INTO proposals
                    (user_id, proposal_type, payload, reason, status, created_at)
                VALUES (?, 'identity_shift', ?, ?, 'pending', ?)
                """,
                (
                    user_id,
                    json.dumps(
                        {
                            "old": old_kardia,
                            "new": new_kardia,
                            "compliance": compliance,
                            "choice": choice,
                            "triggering_observation_timestamp": triggering_observation_timestamp,
                        }
                    ),
                    traj_err,
                    int(time.time()),
                ),
            )
            self._log_axiom_observation(
                user_id, new_kardia, event_type="evolve_attempt",
                outcome="deferred_proposal", old_kardia=old_kardia,
            )
            self.conn.commit()
            return f"Evolution deferred for review: {traj_err}"
        if traj_err and choice == "REWRITE":
            # REWRITE of large magnitude is refused unless explicitly reviewed
            self.cursor.execute(
                """
                INSERT INTO proposals
                    (user_id, proposal_type, payload, reason, status, created_at)
                VALUES (?, 'identity_shift', ?, ?, 'pending', ?)
                """,
                (
                    user_id,
                    json.dumps(
                        {
                            "old": old_kardia,
                            "new": new_kardia,
                            "compliance": compliance,
                            "choice": choice,
                            "triggering_observation_timestamp": triggering_observation_timestamp,
                        }
                    ),
                    f"REWRITE with large delta: {traj_err}",
                    int(time.time()),
                ),
            )
            self._log_axiom_observation(
                user_id, new_kardia, event_type="evolve_attempt",
                outcome="deferred_proposal", old_kardia=old_kardia,
            )
            self.conn.commit()
            return f"REWRITE deferred for review: {traj_err}"

        # Accept the change
        current_time = int(time.time())

        # Record history for future trajectory assessments
        self.cursor.execute(
            """
            INSERT INTO kardia_history
                (user_id, moral_valve, volitional_channel, affective_stance, aesthetic_valve, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                old_kardia["moral_valve"],
                old_kardia["volitional_channel"],
                old_kardia["affective_stance"],
                old_kardia["aesthetic_valve"],
                current_time,
            ),
        )

        self.cursor.execute(
            """
            INSERT INTO active_kardia
                (user_id, moral_valve, volitional_channel,
                 affective_stance, aesthetic_valve, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                moral_valve = excluded.moral_valve,
                volitional_channel = excluded.volitional_channel,
                affective_stance = excluded.affective_stance,
                aesthetic_valve = excluded.aesthetic_valve,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                new_kardia["moral_valve"],
                new_kardia["volitional_channel"],
                new_kardia["affective_stance"],
                new_kardia["aesthetic_valve"],
                current_time,
            ),
        )
        self._log_axiom_observation(
            user_id, new_kardia, event_type="evolve_attempt",
            outcome="applied", old_kardia=old_kardia,
        )
        self.conn.commit()
        return f"Evolution successful. Identity consolidated. Reason: {compliance}"

    # ------------------------------------------------------------------
    # Proposal resolution (deletions + large identity shifts)
    # ------------------------------------------------------------------
    def list_pending_proposals(self, user_id: str) -> List[Dict[str, Any]]:
        self.cursor.execute(
            """
            SELECT id, proposal_type, payload, reason, created_at
            FROM proposals
            WHERE user_id = ? AND status = 'pending'
            ORDER BY created_at ASC
            """,
            (user_id,),
        )
        rows = self.cursor.fetchall()
        return [
            {
                "id": r[0],
                "proposal_type": r[1],
                "payload": json.loads(r[2]),
                "reason": r[3],
                "created_at": r[4],
            }
            for r in rows
        ]

    def resolve_proposal(
        self, user_id: str, proposal_id: int, accept: bool
    ) -> Dict[str, str]:
        """
        Accept or reject a pending proposal.
        - delete_node: on accept, actually deletes the node (+ embedding via cascade)
        - identity_shift: on accept, applies the new Kardia
        """
        self.cursor.execute(
            """
            SELECT proposal_type, payload, status
            FROM proposals
            WHERE id = ? AND user_id = ?
            """,
            (proposal_id, user_id),
        )
        row = self.cursor.fetchone()
        if not row:
            return {"status": "failed", "reason": "Proposal not found."}
        ptype, payload_raw, status = row
        if status != "pending":
            return {"status": "failed", "reason": f"Proposal already {status}."}

        payload = json.loads(payload_raw)
        current_time = int(time.time())

        if not accept:
            self.cursor.execute(
                """
                UPDATE proposals
                SET status = 'rejected', resolved_at = ?
                WHERE id = ?
                """,
                (current_time, proposal_id),
            )
            self.conn.commit()
            return {"status": "success", "message": "Proposal rejected."}

        # Accept path
        if ptype == "delete_node":
            node_id = payload.get("node_id")
            if not node_id:
                return {"status": "failed", "reason": "Malformed delete payload."}

            # Reassess protection and freshness at acceptance time
            self.cursor.execute(
                "SELECT type, asserted_at FROM nodes WHERE user_id = ? AND id = ?",
                (user_id, node_id),
            )
            node_row = self.cursor.fetchone()
            if not node_row:
                # Already gone – treat as resolved
                self.cursor.execute(
                    "UPDATE proposals SET status = 'accepted', resolved_at = ? WHERE id = ?",
                    (current_time, proposal_id),
                )
                self.conn.commit()
                return {"status": "success", "message": "Node already absent; proposal closed."}

            node_type, asserted_at = node_row
            if node_type in self.CORE_PROTECTION_TYPES:
                self.cursor.execute(
                    "UPDATE proposals SET status = 'rejected', resolved_at = ? WHERE id = ?",
                    (current_time, proposal_id),
                )
                self.conn.commit()
                return {
                    "status": "failed",
                    "reason": f"Protected type '{node_type}' cannot be deleted.",
                }

            proposal_ts = payload.get("raw_log_timestamp")
            if (
                proposal_ts is not None
                and asserted_at is not None
                and asserted_at > proposal_ts
            ):
                self.cursor.execute(
                    "UPDATE proposals SET status = 'rejected', resolved_at = ? WHERE id = ?",
                    (current_time, proposal_id),
                )
                self.conn.commit()
                return {
                    "status": "failed",
                    "reason": "Node was updated after the log that proposed deletion; proposal is stale.",
                }

            self.cursor.execute(
                "DELETE FROM nodes WHERE user_id = ? AND id = ?",
                (user_id, node_id),
            )
            self._queue_index_rebuild(user_id)

        elif ptype == "constitutional_observation":
            # Purely informational -- accepting one means "reviewed," not
            # "act on this." No automatic action, no self-modification.
            pass

        elif ptype == "identity_shift":
            new_kardia = payload.get("new")
            if not new_kardia:
                return {"status": "failed", "reason": "Malformed identity payload."}
            old_kardia = self.get_current_kardia(user_id)

            # Re-run destination assessment for safety
            dest_err = self._validate_axioms_destination(new_kardia)
            if dest_err:
                self.cursor.execute(
                    "UPDATE proposals SET status = 'rejected', resolved_at = ? WHERE id = ?",
                    (current_time, proposal_id),
                )
                self._log_within_transaction(
                    user_id, "PROPOSAL_REJECTED_DESTINATION", json.dumps(new_kardia), dest_err
                )
                self._log_axiom_observation(
                    user_id, new_kardia, event_type="proposal_resolution",
                    outcome="rejected_destination",
                )
                self.conn.commit()
                return {"status": "failed", "reason": dest_err}

            # Reassess trajectory against whatever Kardia has become SINCE
            # this proposal was queued -- not just the snapshot compared at
            # creation time. Otherwise an old proposal can land a bigger
            # jump than the inertia gate would allow if evaluated fresh.
            traj_err = self._validate_axioms_trajectory(old_kardia, new_kardia)
            if traj_err:
                self.cursor.execute(
                    "UPDATE proposals SET status = 'rejected', resolved_at = ? WHERE id = ?",
                    (current_time, proposal_id),
                )
                self._log_within_transaction(
                    user_id, "PROPOSAL_REJECTED_STALE_TRAJECTORY", json.dumps(new_kardia),
                    f"No longer within trajectory bounds against current Kardia: {traj_err}",
                )
                self._log_axiom_observation(
                    user_id, new_kardia, event_type="proposal_resolution",
                    outcome="rejected_stale_trajectory", old_kardia=old_kardia,
                )
                self.conn.commit()
                return {
                    "status": "failed",
                    "reason": f"No longer within trajectory bounds against current Kardia: {traj_err}",
                }

            self.cursor.execute(
                """
                INSERT INTO kardia_history
                    (user_id, moral_valve, volitional_channel, affective_stance, aesthetic_valve, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    old_kardia["moral_valve"],
                    old_kardia["volitional_channel"],
                    old_kardia["affective_stance"],
                    old_kardia["aesthetic_valve"],
                    current_time,
                ),
            )
            self.cursor.execute(
                """
                INSERT INTO active_kardia
                    (user_id, moral_valve, volitional_channel,
                     affective_stance, aesthetic_valve, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    moral_valve = excluded.moral_valve,
                    volitional_channel = excluded.volitional_channel,
                    affective_stance = excluded.affective_stance,
                    aesthetic_valve = excluded.aesthetic_valve,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    new_kardia["moral_valve"],
                    new_kardia["volitional_channel"],
                    new_kardia["affective_stance"],
                    new_kardia["aesthetic_valve"],
                    current_time,
                ),
            )
            self._log_axiom_observation(
                user_id, new_kardia, event_type="proposal_resolution",
                outcome="applied", old_kardia=old_kardia,
            )
        else:
            return {"status": "failed", "reason": f"Unknown proposal type: {ptype}"}

        self.cursor.execute(
            """
            UPDATE proposals
            SET status = 'accepted', resolved_at = ?
            WHERE id = ?
            """,
            (current_time, proposal_id),
        )
        self.conn.commit()
        self._flush_index_rebuilds()
        return {"status": "success", "message": f"Proposal {proposal_id} accepted."}

    def close(self) -> None:
        self.conn.close()
