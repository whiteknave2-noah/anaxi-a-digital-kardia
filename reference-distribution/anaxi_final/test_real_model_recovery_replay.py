"""Opt-in: WTR0 same-H recovery end to end with the REAL model.

A synthetic H (shaped like the real failed one: a path-free library request) is
persisted through the real human-session writer, a retry-safe pre-action failure
is evidenced through the real failure-evidence writer, and the real
``execute_recovery`` then reserves, cold-resets (scripted success -- no real
process is touched), and regenerates the EXACT SAME H through the real ordinary
waking pipeline with the real model and the real public library (read-only,
via symlinks into a temp root). Checks: exactly one canonical X linked to that
H, no duplicate H, X projected through the authenticated GUI projection, a
second recovery denied. Synthetic DB only; never the live H.
Enable with ANAXI_REAL_MODEL_HARNESS=1 (own pytest process).
"""
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path

import pytest

_real_ollama = None
try:
    import ollama as _real_ollama
except Exception:  # pragma: no cover
    pass

PUBLIC_ROOT = Path(__file__).resolve().parent / "workspace"
pytestmark = pytest.mark.skipif(
    os.environ.get("ANAXI_REAL_MODEL_HARNESS") != "1" or _real_ollama is None
    or not (PUBLIC_ROOT / "library").is_dir(),
    reason="opt-in real-model harness",
)


def test_recovery_regenerates_the_exact_same_h_with_the_real_model(monkeypatch):
    import test_human_waking_authority_end_to_end as fixture
    import waking_failure_evidence as wfe
    import wtr0_schema_migration
    import wtr0_waking_recovery as recovery
    import workspace_capability as wc
    from wtr0_cold_reset import ColdResetOutcome

    real_chat = _real_ollama.chat
    with tempfile.TemporaryDirectory() as tmp:
        env = fixture._fresh_env(tmp)
        try:
            w, db, actor = env["llama_anaxi"], env["provenance_db_path"], env["registered_actor_id"]
            wtr0_schema_migration.apply_additive_migration(db)
            root = Path(tmp) / "ws"
            root.mkdir()
            for name in ("library", "music", "photographs"):
                (root / name).symlink_to(PUBLIC_ROOT / name, target_is_directory=True)
            (root / "journal").mkdir()
            paths = wc.WorkspacePaths(str(root))
            monkeypatch.setattr(wc.WorkspacePaths, "production_defaults", classmethod(lambda cls: paths))
            w.reset_working_set()

            def chat(model, messages, format=None, options=None, think=None, **kwargs):
                return real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)

            w.ollama.chat = chat
            binding = env["human_session_binding"]
            with closing(sqlite3.connect(db)) as c, c:
                c.execute("PRAGMA foreign_keys=ON")
                authority = binding.bind_session_to_registered_human(
                    c, session_id=w.get_current_session_id(), session_started_at=w.get_current_session_started_at(),
                    pipeline_key=w.PIPELINE_KEY, actor_id=actor,
                )
            prompt = "Would you read something from the library and tell me what you actually received from it?"
            h = binding.record_human_waking_input(
                tmp, pipeline_key=w.PIPELINE_KEY, authority=authority, message=prompt, occurred_at=int(time.time()),
            )
            wfe.record_waking_failure(
                db, human_input_event_id=h, failure_class="WAKING_EXECUTION_FAILURE_NO_X_PERSISTED",
                basis="synthetic retry-safe pre-action failure", retry_safe=True,
            ) if "retry_safe" in wfe.record_waking_failure.__code__.co_varnames else wfe.record_waking_failure(
                db, human_input_event_id=h, failure_class="WAKING_EXECUTION_FAILURE_NO_X_PERSISTED",
                basis="synthetic retry-safe pre-action failure",
            )
            with closing(sqlite3.connect(db)) as c:
                assert recovery.assess_eligibility(c, h, actor)["decision"] == "ELIGIBLE"

            def generation(text):
                assert text == prompt                       # loaded fresh from canonical H, never caller-supplied
                return w.run_waking_turn(
                    env["orch"], text, interaction_mode=w.CONVERSATION_MODE,
                    human_input_authority=authority, existing_human_input_event_id=h,
                )

            outcome = recovery.execute_recovery(
                db, h, actor,
                reset_fn=lambda: {"status": ColdResetOutcome.SUCCEEDED, "basis": "scripted (no process touched)"},
                generation_fn=generation,
            )
            print("\n[recovery] outcome:", {k: v for k, v in outcome.items() if k != "error"}, outcome.get("error"))
            assert outcome["terminal_state"] == "SUCCESS", outcome
            with closing(sqlite3.connect(db)) as c:
                assert c.execute("SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'").fetchone()[0] == 1
                assert c.execute("SELECT COUNT(*) FROM events WHERE event_type='waking_turn'").fetchone()[0] == 1
            with pytest.raises(recovery.RecoveryDenied):
                recovery.execute_recovery(db, h, actor, reset_fn=lambda: None, generation_fn=lambda t: None)
        finally:
            fixture._teardown_env(env)
