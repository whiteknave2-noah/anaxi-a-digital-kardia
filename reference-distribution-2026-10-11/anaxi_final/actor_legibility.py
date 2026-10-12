"""Legible rendering of canonical actor identities in host-delivered evidence (candidate mechanics).

Workspace results carry canonical opaque ids (e.g. a journal entry's ``author_actor_id``).  The substrate should not
have to infer whom an opaque id names.  Where the mapping is canonically established -- the actors table
(actor_type, display_label), or Clark's deterministically derived actor id -- a readable rendering is ADDED beside
the id (``author_actor_id`` stays, ``author_actor`` is added).  Unknown ids get nothing: identity is never guessed.
Real-model finding (Llama 3.1 8B, dev3): with only the opaque id, Clark's own journal entry was attributed to the
human 3/3."""
import os
import sqlite3
import sys

from provenance_schema import derive_stable_id

CLARK_ACTOR_ID = derive_stable_id("actor", "clark")
# PERSPECTIVE-INVARIANT ACTOR RELATION (2026-10-03).  The host has not established what the waking subject is
# called, and second person is ambiguous in a conversation where "you" also addresses it from the human side (live:
# its own prior reply, labelled "your_utterance", was read as the human's).  Canonical identity stays the opaque
# actor id/type; model-facing evidence states only canonical actor EQUALITY with the actor receiving the context.
RECEIVING_ACTOR_ID = CLARK_ACTOR_ID      # every waking context is delivered to this canonical actor
SAME_AS_RECEIVING_ACTOR = "SAME_AS_RECEIVING_ACTOR"
HUMAN_INTERLOCUTOR = "HUMAN_INTERLOCUTOR"
HOST = "HOST"
RELATION_LEGEND = ("actor relation: SAME_AS_RECEIVING_ACTOR = authored/owned by the same canonical actor receiving "
                   "this context now (canonical actor identity only)")
CLARK_RENDERING = SAME_AS_RECEIVING_ACTOR


def enabled():
    la = sys.modules.get("llama_anaxi")
    return bool(getattr(la, "CANDIDATE_PROTOCOL_MECHANICS", False))


def _data_dir():
    la = sys.modules.get("llama_anaxi")
    return getattr(la, "PROVENANCE_DB_DIR", None)


def describe(actor_id, data_dir=None):
    if not isinstance(actor_id, str) or not actor_id:
        return None
    data_dir = data_dir if data_dir is not None else _data_dir()
    row = None
    db = os.path.join(data_dir, "anaxi_provenance.db") if data_dir else None
    if db and os.path.exists(db):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT actor_type, display_label FROM actors WHERE actor_id = ?", (actor_id,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            row = None
    if row is not None:
        actor_type, label = row
        if actor_type == "clark_agent":
            return CLARK_RENDERING
        if actor_type == "human_person":
            return f"{label} (a human principal)" if label else "a human principal (no display name recorded)"
        if actor_type == "host_system":
            return "the ANAXI host"
        return None
    if actor_id == CLARK_ACTOR_ID:
        return CLARK_RENDERING
    return None


def annotate(obj, data_dir=None):
    """A copy of ``obj`` with ``<role>_actor`` added beside every describable ``<role>_actor_id``."""
    if isinstance(obj, list):
        return [annotate(v, data_dir) for v in obj]
    if not isinstance(obj, dict):
        return obj
    out = {}
    for key, value in obj.items():
        out[key] = annotate(value, data_dir)
        if isinstance(key, str) and key.endswith("_actor_id"):
            rendering = describe(value, data_dir)
            if rendering is not None:
                out[key[: -len("_id")]] = rendering
    return out
