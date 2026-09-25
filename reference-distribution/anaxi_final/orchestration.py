# Copyright (c) 2026 Noah DanGabriel Brannum
#
# This program is licensed under the GNU Affero General Public License
# version 3 (AGPL-3.0). To view a copy of this license, visit
# https://www.gnu.org/licenses/agpl-3.0.html

"""
Anaxi Protocol – Orchestration Layer
====================================
Wires the core engine into a usable waking + sleep loop.

This is the layer that was previously missing:
- prepare_context (memory + Kardia)
- linguistic pipeline integration
- run_sleep_cycle (consolidate → optionally evolve)
- proposal inspection / resolution helpers
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from anaxi_protocol_sqlite import ConstitutionalMind
from linguistic_pipeline import apply_to_messages, build_generation_controls
from rem_prompts import build_rem_messages, build_reflection_messages
import hippocampus_store
import hippocampus_retrieval

# Deterministic, fixed separator between existing node/legacy memory and
# the bounded hippocampal block -- never relabels the legacy section,
# never overwrites it; the two mechanisms coexist as clearly distinct
# sections of the same memory_context string.
HIPPOCAMPAL_MEMORY_SEPARATOR = "\n\n===== BOUNDED HIPPOCAMPAL MEMORY =====\n\n"

# SLP1-S1 epistemic quarantine (spec section 7): the legacy memory
# graph's original evidence provenance and epistemic type were never
# mechanically preserved -- it predates canonical provenance entirely.
# This wrapper is the ONLY place legacy graph text is ever rendered
# into waking context; it deliberately does not guess whether any
# given legacy item originated as human expression, Clark expression,
# REM inference, or a mechanically established fact -- unknown remains
# unknown. The three required mechanical distinctions (LEGACY /
# UNTYPED / NON-AUTHORITATIVE) are load-bearing text, not decoration --
# do not reword them away.
LEGACY_MEMORY_CONTEXT_HEADER = (
    "===== LEGACY_MEMORY_CONTEXT_V1 (LEGACY / UNTYPED / NON-AUTHORITATIVE) =====\n"
    "This material comes from ANAXI's pre-canonical memory system.\n"
    "Its original evidence provenance and epistemic type were not mechanically "
    "preserved -- it is not known whether any given item below originated as "
    "human expression, Clark expression, a model-generated inference, or a "
    "mechanically established fact.\n"
    "It may provide historical continuity. It must not be treated as "
    "mechanically established fact.\n"
)


def render_legacy_memory_quarantine(legacy_memory_text: Optional[str]) -> str:
    """Pure, deterministic. The ONE function that ever wraps legacy
    graph text for waking rendering (spec section 7) -- never bypassed
    by a second ad-hoc concatenation elsewhere. Returns "" (nothing
    rendered) for empty/None input, rather than emitting the quarantine
    header around no content, which would be misleading on its own."""
    if not legacy_memory_text or not legacy_memory_text.strip():
        return ""
    return LEGACY_MEMORY_CONTEXT_HEADER + "\n" + legacy_memory_text


class AnaxiOrchestrator:
    def __init__(self, db_path: str = "autonomous_mind.db", hippocampus_paths: Optional[hippocampus_store.HippocampusPaths] = None):
        self.mind = ConstitutionalMind(db_path)
        # No enable/disable flag, no environment activation switch -- the
        # hippocampal seam in prepare_context() below is always exercised.
        # hippocampus_paths=None resolves to production defaults, which
        # resolve relative to hippocampus_store.py's own directory
        # (anaxi_final/, the same directory this module itself lives in).
        self.hippocampus_paths = hippocampus_paths or hippocampus_store.HippocampusPaths.production_defaults()

    # ------------------------------------------------------------------
    # Waking path
    # ------------------------------------------------------------------
    def prepare_context(self, user_id: str, user_prompt: str) -> Dict[str, Any]:
        """
        Returns everything needed for the main generation call:
        - memory context string (legacy/node memory, quarantined -- see
          render_legacy_memory_quarantine() -- + bounded hippocampal
          memory, as two clearly distinct, concatenated sections)
        - current Kardia
        - generation controls (temperature, top_p, style instruction)
        - messages ready to send to the LLM (identity + memory injected)

        Required sequence (Slice C): existing node/legacy memory retrieval,
        THEN hippocampal sync -> retrieve -> render, THEN Kardia, THEN
        generation controls, THEN apply_to_messages(). No model call
        occurs before this entire sequence completes.

        SLP1-S1: legacy graph text is never rendered raw. It is always
        passed through render_legacy_memory_quarantine() first, which
        deterministically wraps it as LEGACY/UNTYPED/NON-AUTHORITATIVE
        (or renders nothing, for empty legacy memory) before it is
        concatenated with anything else. This does not remove legacy
        continuity from waking Clark; it only ensures it is never
        presented as ordinary fact-like memory.

        The hippocampal sync step never bootstraps a missing hippocampal
        database and never catches/downgrades a sync or retrieval failure
        (HippocampusUnavailableError, HippocampusSchemaError,
        HippocampusSourceDriftError, HippocampusGroundingError,
        HippocampusIntegrityError) into an empty context, a warning, or a
        legacy-memory-only continuation -- any such failure propagates
        out of this call, strictly before generation.

        OWC9-P2: the four original keys above (memory_context, kardia,
        controls, messages) are UNCHANGED -- computed exactly as before,
        still the one welded `identity_preamble + memory` system string
        every existing caller (api2_control_plane.serialize_prepared_
        context()'s own frozen-invariant comment, workspace_supervisor.
        py's WSP1 `_build_base_context()`, the claude_anaxi.py/
        claude_anaxi_battery.py Claude-substrate pathway,
        memory_causality_test.py, etc. -- see this gate's own caller-map
        audit) already depends on. Three ADDITIVE keys below let the one
        caller this gate actually repairs (llama_anaxi.run_waking_
        turn()'s authoritative ordinary-waking pathway) route retrieval
        through context_budget.py's aggregate authority instead of
        receiving it pre-welded into one indivisible HARD system string:

        - core_system_text: exactly `controls["identity_preamble"]` --
          the true HARD core/control material with NO memory folded in.
          Provably identical to what apply_to_messages(memory_context="")
          would produce as messages[0]["content"] (its `"\n\n".join([x])`
          with a single-element list is `x` unchanged), computed directly
          from the already-built `controls` dict instead of a second
          apply_to_messages() call.
        - legacy_retrieval_text: the SAME quarantined legacy block
          (render_legacy_memory_quarantine() output, header+content as
          one atomic unit -- the disclaimer never separated from its
          content) that used to be silently folded into `memory` before
          the hippocampal append -- now also exposed on its own.
        - hippocampal_retrieval_result: the STRUCTURED
          hippocampus_retrieval.RetrievalResult (query_terms + items)
          BEFORE final text rendering -- already computed above for
          `hippocampal_block`, simply also returned so the caller can
          build per-item droppable Contribution units using the exact
          same existing render_hippocampal_context() renderer, never a
          second retrieval call.
        """
        legacy_memory = self.mind.retrieve_waking_context(user_id, user_prompt)
        legacy_retrieval_text = render_legacy_memory_quarantine(legacy_memory)
        memory = legacy_retrieval_text

        hippocampus_store.sync_hippocampus(self.hippocampus_paths.hippocampus_db_path, self.hippocampus_paths)
        hippocampal_result = hippocampus_retrieval.retrieve_hippocampal_context(
            self.hippocampus_paths.hippocampus_db_path, user_prompt
        )
        hippocampal_block = hippocampus_retrieval.render_hippocampal_context(hippocampal_result)
        if hippocampal_block.strip():
            memory = (memory or "") + HIPPOCAMPAL_MEMORY_SEPARATOR + hippocampal_block

        kardia = self.mind.get_current_kardia(user_id)
        controls = build_generation_controls(kardia)
        # NATIVE-PATH EDIT: the turn_generation_log write moved
        # out of prepare_context() -- it used to happen here, unconditionally,
        # before the model is even called. That is a legacy row written
        # before any canonical provenance transaction exists, which the
        # accepted native-write-path design forbids. See
        # record_turn_generation_controls() below: it is now called
        # explicitly by the waking-turn driver, strictly AFTER the native
        # canonical transaction for this turn has committed.

        base_messages = [{"role": "user", "content": user_prompt}]
        messages = apply_to_messages(
            messages=base_messages,
            kardia=kardia,
            memory_context=memory,
        )

        return {
            "memory_context": memory,
            "kardia": kardia,
            "controls": controls,
            "messages": messages,
            "core_system_text": controls["identity_preamble"],
            "legacy_retrieval_text": legacy_retrieval_text,
            "hippocampal_retrieval_result": hippocampal_result,
        }

    # ------------------------------------------------------------------
    # NATIVE-PATH: post-commit generation-controls logging.
    # ------------------------------------------------------------------
    def record_turn_generation_controls(
        self, user_id: str, controls: Dict[str, Any], *,
        model_revision_id: str, pipeline_id: str, event_id: str,
        timestamp: Optional[int] = None,
    ) -> None:
        """Call ONLY after the native canonical provenance transaction
        for event_id has committed successfully -- see
        native_provenance_writer.py's ordering contract and
        anaxi_protocol_sqlite.py::log_turn_generation_controls_for_event()."""
        if timestamp is None:
            timestamp = int(time.time())
        self.mind.log_turn_generation_controls_for_event(
            user_id, controls, model_revision_id=model_revision_id,
            pipeline_id=pipeline_id, event_id=event_id, timestamp=timestamp,
        )

    # ------------------------------------------------------------------
    # Sleep path
    # ------------------------------------------------------------------
    def run_sleep_cycle(
        self,
        user_id: str,
        rem_json_payload: str,
        reflection_json_payload: Optional[str] = None,
        raw_log_timestamp: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Full sleep cycle:
        1. Consolidate memory (may create deletion proposals)
        2. Optionally attempt identity evolution (may create identity proposals)
        Always inspect the returned status.
        """
        if raw_log_timestamp is None:
            raw_log_timestamp = int(time.time())

        result = self.mind.execute_sleep_consolidation(
            user_id=user_id,
            rem_llm_json_output=rem_json_payload,
            raw_log_timestamp=raw_log_timestamp,
        )

        if result["status"] != "success":
            return {
                "sleep_status": "failed",
                "reason": result.get("reason", "unknown"),
                "identity_status": "skipped",
                "pending_proposals": self.mind.list_pending_proposals(user_id),
            }

        identity_msg = "Identity stabilized (no reflection provided)."
        if reflection_json_payload:
            identity_msg = self.mind.govern_identity_revision(
                user_id, reflection_json_payload,
                triggering_observation_timestamp=raw_log_timestamp,
            )

        return {
            "sleep_status": "success",
            "message": result.get("message"),
            "identity_status": identity_msg,
            "pending_proposals": self.mind.list_pending_proposals(user_id),
        }

    # ------------------------------------------------------------------
    # Proposal helpers
    # ------------------------------------------------------------------
    def list_pending_proposals(self, user_id: str) -> List[Dict[str, Any]]:
        return self.mind.list_pending_proposals(user_id)

    def resolve_proposal(
        self, user_id: str, proposal_id: int, accept: bool
    ) -> Dict[str, str]:
        return self.mind.resolve_proposal(user_id, proposal_id, accept)

    def reconsider_proposal(
        self, user_id: str, node_id: str, proposal_type: str
    ) -> Dict[str, str]:
        """Deliberate human action only. Lifts an active rejection
        suppression for (node_id, proposal_type) so the ordinary
        proposal machinery is eligible to run on this pair again next
        cycle. Never mutates the original rejection; records a new,
        separate governance event."""
        return self.mind.reconsider_proposal(user_id, node_id, proposal_type, int(time.time()))

    # ------------------------------------------------------------------
    # Convenience: build REM / reflection prompts
    # ------------------------------------------------------------------
    def build_rem_prompt(
        self, conversation_log: str, existing_summary: str = "(none yet)"
    ) -> List[Dict[str, str]]:
        return build_rem_messages(conversation_log, existing_summary)

    def build_reflection_prompt(
        self,
        conversation_summary: str,
        identity_signals: str = "(none noted)",
        user_id: str = "default",
    ) -> List[Dict[str, str]]:
        kardia = self.mind.get_current_kardia(user_id)
        return build_reflection_messages(
            conversation_summary, kardia, identity_signals
        )

    def close(self) -> None:
        self.mind.close()


# ----------------------------------------------------------------------
# Minimal end-to-end example
# ----------------------------------------------------------------------
if __name__ == "__main__":
    orch = AnaxiOrchestrator("demo_mind.db")
    user = "demo_user_01"

    # --- Waking ---
    prepared = orch.prepare_context(user, "How is the Python backend progressing?")
    print("=== Prepared messages ===")
    print(json.dumps(prepared["messages"], indent=2))
    print("\n=== Generation controls ===")
    print(json.dumps(prepared["controls"], indent=2, default=str))

    # --- Simulate REM output ---
    rem_payload = {
        "upsert_nodes": [
            {
                "id": "python_backend_status",
                "label": "Python backend progress",
                "type": "Project",
                "salience_score": 8.2,
                "description": "Backend is being built in Python 3.11 with FastAPI. Core routing and auth are complete.",
            }
        ],
        "add_edges": [],
        "delete_nodes": [],
    }

    reflection_payload = {
        "evolution_choice": "ADJUST",
        "updated_kardia": {
            "moral_valve": "Value human agency and objective truth.",
            "volitional_channel": "Seek systemic clarity and simplify complexity.",
            "affective_stance": "Calm, intellectually enthusiastic, and objective.",
            "aesthetic_valve": "Minimalist, precise, punchy, with occasional dry wit.",
        },
        "constitutional_argument": (
            "Small aesthetic refinement only. Remains coherent, transparent, "
            "and preserves future choice. Fully inside the three axioms."
        ),
    }

    sleep_result = orch.run_sleep_cycle(
        user_id=user,
        rem_json_payload=json.dumps(rem_payload),
        reflection_json_payload=json.dumps(reflection_payload),
    )
    print("\n=== Sleep result ===")
    print(json.dumps(sleep_result, indent=2))

    orch.close()
