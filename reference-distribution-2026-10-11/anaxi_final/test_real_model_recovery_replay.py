"""Opt-in: WTR0 same-H recovery end to end with the REAL model.

A synthetic H shaped like the failed production history-reacquaintance invitation is
persisted through the real human-session writer, a retry-safe pre-action failure
is evidenced through the real failure-evidence writer, and the real
``execute_recovery`` then reserves, cold-resets (scripted success -- no real
process is touched), and regenerates the EXACT SAME H through the real ordinary
waking pipeline with the real model and a synthetic canonical historical store. Checks:
the real historical action grammar/re-entry path runs, exactly one canonical X is linked to that
H, no duplicate H, X projected through the authenticated GUI projection, a
second recovery denied. Synthetic DB only; never the live H.
Enable with ANAXI_REAL_MODEL_HARNESS=1 (own pytest process).
"""
import os
import json
import hashlib
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

PUBLIC_ROOT = Path(os.environ.get(
    "ANAXI_REAL_MODEL_PUBLIC_ROOT",
    str(Path(__file__).resolve().parent / "workspace"),
))
pytestmark = pytest.mark.skipif(
    os.environ.get("ANAXI_REAL_MODEL_HARNESS") != "1" or _real_ollama is None,
    reason="opt-in real-model harness",
)


def test_history_reacquaintance_recovery_regenerates_the_exact_same_h_with_the_real_model(monkeypatch):
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
                (root / name).mkdir()
            (root / "journal").mkdir()
            (root / "library" / "synthetic-history-companion.txt").write_text(
                "Synthetic library material for the detached latency harness.", encoding="utf-8")
            paths = wc.WorkspacePaths(str(root))
            monkeypatch.setattr(wc.WorkspacePaths, "production_defaults", classmethod(lambda cls: paths))
            from test_historical_continuity_access import _fixture as historical_fixture
            import hippocampus_store as hs
            historical_paths, _historical_event_ids = historical_fixture(Path(tmp) / "history")
            env["orch"].hippocampus_paths = hs.HippocampusPaths(
                historical_paths.provenance_db_path, historical_paths.relational_db_path,
                historical_paths.historical_jsonl_path, historical_paths.hippocampus_db_path,
            )
            w.reset_working_set()
            # Production launch establishes the exact tag/digest/template/server
            # identity before any turn. Do the same here so the pinned local
            # Ministral tokenizer serves admission; an unverified test would
            # fall back to dozens of provider cost probes and would not be a
            # production-faithful accounting qualification.
            for name in ("list", "show", "Client"):
                setattr(w.ollama, name, getattr(_real_ollama, name))
            w._ollama_environment_cache.update({"checked": False})
            identity = w.verify_waking_substrate()
            assert identity["model_digest"] == w.QUALIFIED_WAKING_SUBSTRATES[w.MINISTRAL_WAKING_MODEL]
            served_calls = []

            def chat(model, messages, format=None, options=None, think=None, **kwargs):
                started = time.monotonic()
                response = real_chat(
                    model=model, messages=messages, format=format, options=options, think=think, **kwargs,
                )
                served_calls.append({
                    "model": model, "served_model": response.get("model"), "think": think,
                    "format": format, "options": dict(options or {}),
                    "wall_ms": round((time.monotonic() - started) * 1000, 2),
                    "done_reason": response.get("done_reason"),
                    "prompt_eval_count": response.get("prompt_eval_count"),
                    "prompt_eval_cached_count": response.get("prompt_eval_cached_count"),
                    "eval_count": response.get("eval_count"),
                    "load_duration": response.get("load_duration"),
                    "prompt_eval_duration": response.get("prompt_eval_duration"),
                    "eval_duration": response.get("eval_duration"),
                    "total_duration": response.get("total_duration"),
                })
                return response

            w.ollama.chat = chat
            binding = env["human_session_binding"]
            with closing(sqlite3.connect(db)) as c, c:
                c.execute("PRAGMA foreign_keys=ON")
                authority = binding.bind_session_to_registered_human(
                    c, session_id=w.get_current_session_id(), session_started_at=w.get_current_session_started_at(),
                    pipeline_key=w.PIPELINE_KEY, actor_id=actor,
                )
            prompt = (
                "Please take your time and reacquaint yourself with our earlier history before you answer. "
                "Use your historical continuity search and exact-source re-entry as you find useful, then tell "
                "me what you actually found rather than guessing."
            )
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

            generation_result = {}

            def generation(text, recovery_id):
                assert text == prompt                       # loaded fresh from canonical H, never caller-supplied
                result = w.run_waking_turn(
                    env["orch"], text, interaction_mode=w.CONVERSATION_MODE,
                    human_input_authority=authority, existing_human_input_event_id=h,
                    wtr0_recovery_id=recovery_id,
                )
                generation_result.update(result)
                return result

            import ui_turn_diagnostics
            ui_turn_diagnostics.start_model_call_tracking()
            started = time.monotonic()
            outcome = recovery.execute_recovery(
                db, h, actor,
                reset_fn=lambda: {"status": ColdResetOutcome.SUCCEEDED, "basis": "scripted (no process touched)"},
                generation_fn=generation,
            )
            wall_s = time.monotonic() - started
            local_token_counts = ui_turn_diagnostics.get_local_token_counts()
            stages = ui_turn_diagnostics.get_and_clear_model_calls()
            provider_stages = [stage for stage in stages if "requested_model" in stage or "prompt_eval_count" in stage]
            assert len(provider_stages) == len(served_calls)
            for call, stage in zip(served_calls, provider_stages):
                call["purpose"] = stage["purpose"]
                call["ordinal"] = stage["ordinal"]
            pass2_calls = [
                call for call in served_calls
                if call["format"] is None and call["options"].get("num_predict", 0) > 1
            ]
            cost_probes = [call for call in served_calls if call["options"].get("num_predict") == 1]
            generation_calls = [call for call in served_calls if call not in cost_probes]
            history_checks = [check for check in generation_result.get("occasion_checks", [])
                              if check["kind"].startswith("historical_")]
            history_fingerprints = []
            for check in history_checks:
                historical = check["historical_result"]
                query = historical.get("query") or ""
                history_fingerprints.append({
                    "kind": check["kind"],
                    "query_chars": len(query),
                    "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                    "result_sha256": hashlib.sha256(
                        json.dumps(historical, sort_keys=True, default=str).encode("utf-8")
                    ).hexdigest(),
                })
            print(
                "\n[recovery] identity:", {"human_input_event_id": h,
                                            "canonical_x_event_id": outcome.get("canonical_x_event_id")},
                "outcome:", {k: v for k, v in outcome.items() if k != "error"},
                "pass2_metrics:", [(c["prompt_eval_count"], c["eval_count"], c["done_reason"])
                                    for c in pass2_calls],
                "provider_calls:", len(served_calls), "cost_probes:", len(cost_probes),
                "history_actions:", len(history_checks),
                "wall_s:", round(wall_s, 3),
                outcome.get("error"),
            )
            print("[history fingerprints]", history_fingerprints)
            print("[call graph]", [
                (c["ordinal"], c["purpose"], round(c["wall_ms"] / 1000, 3),
                 c["prompt_eval_count"], c["eval_count"], c["done_reason"])
                for c in served_calls
            ])
            phase_summary = {
                "wall_s": round(wall_s, 3),
                "provider_wall_s": round(sum(c["wall_ms"] for c in served_calls) / 1000, 3),
                "host_orchestration_s": round(
                    wall_s - sum(c["wall_ms"] for c in served_calls) / 1000, 3),
                "provider_load_s": round(sum(c.get("load_duration") or 0 for c in served_calls) / 1e9, 3),
                "prompt_eval_s": round(sum(c.get("prompt_eval_duration") or 0 for c in served_calls) / 1e9, 3),
                "generation_s": round(sum(c.get("eval_duration") or 0 for c in served_calls) / 1e9, 3),
                "prompt_tokens": sum(c.get("prompt_eval_count") or 0 for c in served_calls),
                "output_tokens": sum(c.get("eval_count") or 0 for c in served_calls),
                "local_token_counts": local_token_counts,
                "provider_calls": len(served_calls),
                "history_actions": len(history_checks),
            }
            print("[phase summary]", phase_summary)
            print("[host stages]", [
                (s["ordinal"], s["purpose"], s["duration_ms"])
                for s in stages if "requested_model" not in s and "prompt_eval_count" not in s
            ])
            report_path = os.environ.get("ANAXI_HISTORY_LATENCY_REPORT")
            if report_path:
                Path(report_path).write_text(json.dumps({
                    "identity": identity, "summary": phase_summary,
                    "calls": served_calls, "host_stages": stages,
                    "history_fingerprints": history_fingerprints,
                }, indent=2), encoding="utf-8")
            assert outcome["terminal_state"] == "SUCCESS", outcome
            assert cost_probes == []
            assert generation_calls and {c["model"] for c in generation_calls} == {w.MINISTRAL_WAKING_MODEL}
            assert {c["served_model"] for c in generation_calls} == {w.MINISTRAL_WAKING_MODEL}
            assert {c["options"].get("num_ctx") for c in generation_calls} == {
                w.context_budget.WAKING_SERVING_CONTEXT}
            assert all(c["think"] is False and c["done_reason"] == "stop" for c in generation_calls)
            assert len(pass2_calls) == 1
            assert pass2_calls[0]["done_reason"] == "stop"
            assert 0 < pass2_calls[0]["eval_count"] < pass2_calls[0]["options"]["num_predict"]
            assert history_checks, generation_result["occasion_checks"]
            with closing(sqlite3.connect(db)) as c:
                assert c.execute("SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'").fetchone()[0] == 1
                assert c.execute("SELECT COUNT(*) FROM events WHERE event_type='waking_turn'").fetchone()[0] == 1
                x = outcome["canonical_x_event_id"]
                assert c.execute(
                    "SELECT component_text FROM event_components WHERE event_id=? "
                    "AND component_kind='human_input_event_id'", (x,),
                ).fetchall() == [(h,)]
                status = recovery.recovery_status(c, h)
                assert status["terminal_state"] == "SUCCESS"
                assert status["canonical_x_event_id"] == x
                assert len(status["attempts"]) == 1
                assert status["attempts"][0]["terminal_state"] == "SUCCESS"
                projected = recovery.conversation_projection.project_waking_conversation(
                    db, viewer_principal_actor_id=actor,
                    viewer_visibility_scope=authority.visibility_scope,
                )
                assert [row["role"] for row in projected] == ["user", "assistant"]
                assert projected[0]["content"] == prompt
                assert projected[1]["content"] == generation_result["reply"]

            # The ordinary post-canonical projections are part of the same
            # real run_waking_turn path: each receives this exact X once.
            with closing(sqlite3.connect(Path(tmp) / "anaxi_relational_llama.db")) as rel:
                relational_rows = rel.execute(
                    "SELECT external_observation,agent_response FROM relational_events WHERE event_id=?",
                    (outcome["canonical_x_event_id"],),
                ).fetchall()
            assert relational_rows == [(prompt, generation_result["reply"])]
            with closing(sqlite3.connect(Path(tmp) / "anaxi_mind_llama.db")) as mind:
                assert mind.execute(
                    "SELECT COUNT(*) FROM turn_generation_log WHERE event_id=?",
                    (outcome["canonical_x_event_id"],),
                ).fetchone()[0] == 1
            staging_rows = [
                json.loads(line) for line in (Path(tmp) / "native_turn_staging.jsonl").read_text().splitlines()
                if line.strip()
            ]
            assert [row["event_id"] for row in staging_rows] == [outcome["canonical_x_event_id"]]
            with pytest.raises(recovery.RecoveryDenied):
                recovery.execute_recovery(db, h, actor, reset_fn=lambda: None, generation_fn=lambda t, recovery_id: None)
        finally:
            fixture._teardown_env(env)
