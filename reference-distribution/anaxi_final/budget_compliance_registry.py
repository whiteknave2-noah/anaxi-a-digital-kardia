"""OWC9-P4 section 8: the smallest maintainable source representation
of every current production local-model inference path and its budget
status. A plain constant table, not a runtime service (spec: "Do not
build a runtime service merely for this").

This is the single source of truth cross-checked by test_owc9p4_
bypass_guard.py's own compliance-registry tests: every real, AST-
discoverable `ollama.chat(model=...)` call site anywhere in a scanned
production file must be reachable via WRAPPER_CALL_SITES below, and
every entry in WRAPPER_CALL_SITES must still be genuinely discoverable
in source -- the two can never silently drift apart in either
direction (OWC9-P4A section 3's own bidirectional cross-check).

Each row:
    path_id: a short, stable identifier for this inference path.
    file / function: the file/function this path is INITIATED from
        (for human readability -- e.g. "workspace_roaming.py" for
        roaming Stage-1, even though the actual ollama.chat() call
        physically executes inside llama_anaxi.py's own
        ask_llama_for_json(), which workspace_roaming.py calls through
        a passed-in parameter, never by importing/calling ollama
        itself). NOT what the bypass guard's AST scan cross-checks --
        see wrapper_call_site for that.
    wrapper_call_site: the exact (file, function) pair where the real
        ollama.chat(model=...) call physically lives -- always one of
        the small, fixed set of approved wrapper functions
        (llama_anaxi.ask_llama_for_json, llama_anaxi.call_llama,
        llama_sleep.ask_llama_for_json). Multiple path rows legitimately
        share the same wrapper_call_site (e.g. conversation_pass1,
        artifact_judgment, wsp1_pass1, roaming_stage1, and
        roaming_stage2_public all physically call through
        llama_anaxi.ask_llama_for_json) -- this is exactly why
        WRAPPER_CALL_SITES below is a SET, not a list.
    model: the substrate this path calls.
    budget_profile: the context_budget.py constant pair (or "N/A")
        this path's aggregate prompt is checked against.
    cost_method: "byte_estimate" (context_budget.estimate_tokens, the
        conservative UTF-8-byte upper bound) or "calibrated" (the
        fingerprint-guarded calibrated_or_fallback_cost() mechanism,
        with automatic fallback to byte_estimate on any mismatch).
    preflight_owner: the function that composes/checks the budget
        immediately before this path's model call.
    enabled: whether this path is currently reachable in production
        (a scheduled task being OS-level Disabled does not change this
        field -- code-level reachability is what this tracks; see
        `reachability_note` for the operational detail).
    reachability_note: free text -- how/when this path is actually
        reached in production.
"""

CONVERSATION_PASS1 = {
    "path_id": "conversation_pass1",
    "file": "llama_anaxi.py",
    "function": "ask_llama_for_json",
    "wrapper_call_site": ("llama_anaxi.py", "ask_llama_for_json"),
    "model": "gemma4:e4b",
    "budget_profile": "PASS1_MAX_PROMPT_BUDGET / PASS1_GENERATION_RESERVE",
    "cost_method": "calibrated (immutable task+closing-cue scaffold), byte_estimate (everything else)",
    "preflight_owner": "run_waking_turn() -- pass1_result = compose_within_budget(...)",
    "enabled": True,
    "reachability_note": "Every ordinary CONVERSATION-mode waking turn.",
}

CONVERSATION_PASS2 = {
    "path_id": "conversation_pass2",
    "file": "llama_anaxi.py",
    "function": "call_llama",
    "wrapper_call_site": ("llama_anaxi.py", "call_llama"),
    "model": "gemma4:e4b",
    "budget_profile": "PASS2_MAX_PROMPT_BUDGET / PASS2_GENERATION_RESERVE",
    "cost_method": "calibrated (immutable task+closing-cue scaffold), byte_estimate (everything else)",
    "preflight_owner": "run_waking_turn() -- pass2_result = compose_within_budget(...)",
    "enabled": True,
    "reachability_note": "Every ordinary CONVERSATION-mode waking turn.",
}

TASK_PASS2_TASK_MODE = {
    "path_id": "task_pass2_task_mode",
    "file": "llama_anaxi.py",
    "function": "call_llama",
    "wrapper_call_site": ("llama_anaxi.py", "call_llama"),
    "model": "gemma4:e4b",
    "budget_profile": "PASS2_MAX_PROMPT_BUDGET / PASS2_GENERATION_RESERVE (reused -- same free-form-reply output shape)",
    "cost_method": "byte_estimate",
    "preflight_owner": "run_waking_turn() -- _compose_task_mode_pass2_budget()",
    "enabled": True,
    "reachability_note": "Every TASK-mode waking turn (the default interaction mode).",
}

TASK_PASS2_REGEN = {
    "path_id": "task_pass2_regen",
    "file": "llama_anaxi.py",
    "function": "call_llama",
    "wrapper_call_site": ("llama_anaxi.py", "call_llama"),
    "model": "gemma4:e4b",
    "budget_profile": "PASS2_MAX_PROMPT_BUDGET / PASS2_GENERATION_RESERVE (reused)",
    "cost_method": "byte_estimate",
    "preflight_owner": "run_waking_turn() -- _compose_task_mode_pass2_budget()",
    "enabled": True,
    "reachability_note": "TASK-mode turns only, only when the free-prose screen flags a match on the first reply.",
}

ARTIFACT_JUDGMENT = {
    "path_id": "artifact_judgment",
    "file": "llama_anaxi.py",
    "function": "ask_llama_for_json",
    "wrapper_call_site": ("llama_anaxi.py", "ask_llama_for_json"),
    "model": "gemma4:e4b",
    "budget_profile": "WSP1_PASS1_MAX_PROMPT_BUDGET / WSP1_PASS1_GENERATION_RESERVE (reused -- same unbounded-content-JSON output class)",
    "cost_method": "calibrated (immutable system+closing-cue scaffold, OWC9-P4A), byte_estimate (dynamic content)",
    "preflight_owner": "run_waking_turn() -- _compose_artifact_judgment_budget()",
    "enabled": True,
    "reachability_note": "Both CONVERSATION and TASK mode, only when signal_matcher.classify_signal() returns POSITIVE.",
}

WSP1_PASS1 = {
    "path_id": "wsp1_pass1",
    "file": "workspace_supervisor.py",
    "function": "ask_llama_for_json (via llama_anaxi)",
    "wrapper_call_site": ("llama_anaxi.py", "ask_llama_for_json"),
    "model": "gemma4:e4b",
    "budget_profile": "WSP1_PASS1_MAX_PROMPT_BUDGET / WSP1_PASS1_GENERATION_RESERVE",
    "cost_method": "byte_estimate",
    "preflight_owner": "workspace_supervisor.py's own compose_within_budget() call (pre-existing, OWC9-P2/P3)",
    "enabled": True,
    "reachability_note": "WSP1 supervised-workspace-action turns.",
}

WSP1_PASS2 = {
    "path_id": "wsp1_pass2",
    "file": "workspace_supervisor.py",
    "function": "call_llama (via llama_anaxi)",
    "wrapper_call_site": ("llama_anaxi.py", "call_llama"),
    "model": "gemma4:e4b",
    "budget_profile": "WSP1_PASS2_MAX_PROMPT_BUDGET / WSP1_PASS2_GENERATION_RESERVE",
    "cost_method": "byte_estimate",
    "preflight_owner": "workspace_supervisor.py's own compose_within_budget() call (pre-existing, OWC9-P2/P3)",
    "enabled": True,
    "reachability_note": (
        "WSP1 supervised-workspace-action turns whose action is NOT a "
        "genuine photograph VIEW (CAP2E: a photograph-VIEW turn routes "
        "this exact same wrapper_call_site to VISION_MODEL instead -- "
        "see WSP1_PASS2_VISION below, a distinct registered path, not "
        "a second physical call site)."
    ),
}

WSP1_PASS2_VISION = {
    "path_id": "wsp1_pass2_vision",
    "file": "workspace_supervisor.py",
    "function": "call_llama (via llama_anaxi)",
    "wrapper_call_site": ("llama_anaxi.py", "call_llama"),
    "model": "qwen3-vl:4b",
    "budget_profile": "QWEN_VISION_MAX_PROMPT_BUDGET / QWEN_VISION_GENERATION_RESERVE (model-specific -- independently measured for qwen3-vl:4b, never reused from gemma4:e4b's own WSP1_PASS2 profile by analogy)",
    "cost_method": "byte_estimate (dynamic text); qwen_image_admission_cost() (the admitted photograph itself -- area-aware, not a flat constant; see context_budget.py)",
    "preflight_owner": "workspace_supervisor.py's own compose_within_budget() call, selecting this profile only when a genuine admitted image is present (CAP2E)",
    "enabled": True,
    "reachability_note": (
        "WSP1 supervised-workspace-action turns whose action IS a "
        "genuine photograph VIEW that the host actually delivered bytes "
        "for (CAP2D-P1 proved qwen3-vl:4b, not gemma4:e4b, grounds on "
        "pixels on this installation). SAME physical wrapper_call_site "
        "as wsp1_pass2 above -- llama_anaxi.call_llama()'s own `model` "
        "parameter, not a second ollama.chat() call site anywhere in "
        "source; the bypass guard's AST scan finds exactly one chat() "
        "call inside call_llama() regardless of which model string is "
        "passed to it at runtime."
    ),
}

ROAMING_STAGE1 = {
    "path_id": "roaming_stage1",
    "file": "workspace_roaming.py",
    "function": "ask_llama_for_json (via llama_anaxi)",
    "wrapper_call_site": ("llama_anaxi.py", "ask_llama_for_json"),
    "model": "gemma4:e4b",
    "budget_profile": "ROAMING_MAX_PROMPT_BUDGET / ROAMING_GENERATION_RESERVE",
    "cost_method": "byte_estimate",
    "preflight_owner": "_compose_roaming_stage1_budget()",
    "enabled": True,
    "reachability_note": "Unattended roaming decision loop, when authorized.",
}

ROAMING_STAGE2_PUBLIC = {
    "path_id": "roaming_stage2_public",
    "file": "workspace_roaming.py",
    "function": "ask_llama_for_json (via llama_anaxi)",
    "wrapper_call_site": ("llama_anaxi.py", "ask_llama_for_json"),
    "model": "gemma4:e4b",
    "budget_profile": "WSP1_PASS1_MAX_PROMPT_BUDGET / WSP1_PASS1_GENERATION_RESERVE (reused -- identical schema)",
    "cost_method": "byte_estimate (entirely fixed content -- no dynamic material feeds this prompt at all)",
    "preflight_owner": "compose_workspace_action_stage2_budget()",
    "enabled": True,
    "reachability_note": "Roaming Stage-1 selects roaming_act='act'.",
}

ROAMING_STAGE2_PRIVATE = {
    "path_id": "roaming_stage2_private",
    "file": "workspace_private.py",
    "function": "ask_llama_for_json (via llama_anaxi)",
    "wrapper_call_site": ("llama_anaxi.py", "ask_llama_for_json"),
    "model": "gemma4:e4b",
    "budget_profile": "WSP1_PASS1_MAX_PROMPT_BUDGET / WSP1_PASS1_GENERATION_RESERVE (reused -- STAGE2_PRIVATE_ACTION_SCHEMA is the documented exact sibling of STAGE2_ACTION_SCHEMA)",
    "cost_method": "byte_estimate; last-private-observation is its own SOFT/droppable contribution",
    "preflight_owner": "workspace_private.compose_private_action_budget()",
    "enabled": True,
    "reachability_note": "Roaming Stage-1 selects roaming_act='private_act', and a private root is configured.",
}

SLEEP_REM_CONSOLIDATION = {
    "path_id": "sleep_rem_consolidation",
    "file": "llama_sleep.py",
    "function": "ask_llama_for_json",
    "wrapper_call_site": ("llama_sleep.py", "ask_llama_for_json"),
    "model": "llama3.2:3b",
    "budget_profile": "SLEEP_REM_MAX_PROMPT_BUDGET / SLEEP_REM_GENERATION_RESERVE (model-specific -- SLEEP_LLAMA_CONTEXT_CEILING, independently confirmed via ollama.show(), not assumed from gemma4:e4b)",
    "cost_method": "byte_estimate",
    "preflight_owner": "llama_sleep._compose_rem_budget()",
    "enabled": True,
    "reachability_note": (
        "SLP1-E: llama_sleep.py's own CLI (`python llama_sleep.py`, with or "
        "without arguments) is now mechanically disabled -- main() prints "
        "LEGACY_SLEEP_DISABLED_MESSAGE and exits before constructing "
        "AnaxiOrchestrator or reaching run_sleep(), so neither manual "
        "invocation nor the (already OS-level Disabled) 'Anaxi Sleep Cycle' "
        "Windows Scheduled Task can reach this path anymore. run_sleep() "
        "itself is unchanged and remains directly callable only by code that "
        "imports this module under its own control (e.g. "
        "test_sleep_receipts.py's existing regression coverage, which never "
        "touches real data) -- kept budgeted rather than removed because "
        "that coverage still exercises this exact preflight."
    ),
}

SLEEP_IDENTITY_REFLECTION = {
    "path_id": "sleep_identity_reflection",
    "file": "llama_sleep.py",
    "function": "ask_llama_for_json",
    "wrapper_call_site": ("llama_sleep.py", "ask_llama_for_json"),
    "model": "llama3.2:3b",
    "budget_profile": "SLEEP_REFLECTION_MAX_PROMPT_BUDGET / SLEEP_REFLECTION_GENERATION_RESERVE (model-specific)",
    "cost_method": "byte_estimate",
    "preflight_owner": "llama_sleep._compose_reflection_budget()",
    "enabled": True,
    "reachability_note": "Same reachability as sleep_rem_consolidation -- the same run_sleep() cycle.",
}

SLEEP_SELECTION = {
    "path_id": "sleep_selection",
    "file": "sleep_selection.py",
    "function": "ask_llama_for_json (via llama_sleep)",
    "wrapper_call_site": ("llama_sleep.py", "ask_llama_for_json"),
    "model": "llama3.2:3b",
    "budget_profile": "SLEEP_SELECTION_MAX_PROMPT_BUDGET / SLEEP_SELECTION_GENERATION_RESERVE (model-specific -- SLEEP_LLAMA_CONTEXT_CEILING, same independently-confirmed ceiling as sleep_rem_consolidation/sleep_identity_reflection)",
    "cost_method": "byte_estimate",
    "preflight_owner": "sleep_selection.py's own _compose_selection_budget()",
    "enabled": True,
    "reachability_note": (
        "SLP1-B Sleep Selection -- reached from the owner-surface Sleep "
        "control via the sleep_cycle production-defaults entrypoint "
        "(after owner authorization and explicit confirmation); not called "
        "by any scheduler or by llama_sleep.run_sleep(). "
        "Reuses llama_sleep.ask_llama_for_json's already-approved wrapper "
        "call site directly -- no new physical ollama.chat() call site is "
        "introduced anywhere in source. Covers BOTH initial and "
        "reduction-mode Selection calls: reduction is the same pathway, "
        "substrate, and budget profile as initial selection, never a "
        "second inference path (see sleep_selection.py's module docstring)."
    ),
}

SLEEP_TRANSFORMATION = {
    "path_id": "sleep_transformation",
    "file": "sleep_transformation.py",
    "function": "ask_llama_for_json (via llama_sleep)",
    "wrapper_call_site": ("llama_sleep.py", "ask_llama_for_json"),
    "model": "llama3.2:3b",
    "budget_profile": "SLEEP_TRANSFORMATION_MAX_PROMPT_BUDGET / SLEEP_TRANSFORMATION_GENERATION_RESERVE (model-specific -- SLEEP_LLAMA_CONTEXT_CEILING, same independently-confirmed ceiling as sleep_rem_consolidation/sleep_identity_reflection/sleep_selection)",
    "cost_method": "byte_estimate",
    "preflight_owner": "sleep_transformation.py's own _compose_transformation_budget()",
    "enabled": True,
    "reachability_note": (
        "SLP1-C Sleep Transformation -- reached from the owner-surface Sleep "
        "control via the sleep_cycle production-defaults entrypoint "
        "(after owner authorization and explicit confirmation); not called "
        "by any scheduler or by llama_sleep.run_sleep(). Reuses llama_sleep.ask_llama_for_json's "
        "already-approved wrapper call site directly -- no new physical "
        "ollama.chat() call site is introduced anywhere in source."
    ),
}

REAL_TEXT_COST_RECOVERY_PROBE = {
    "path_id": "real_text_cost_recovery_probe",
    "file": "llama_anaxi.py",
    "function": "_measure_real_text_cost",
    "wrapper_call_site": ("llama_anaxi.py", "_measure_real_text_cost"),
    "model": "gemma4:e4b",
    "budget_profile": "CONTEXT_CEILING / num_predict=1 / SAFETY_MARGIN",
    "cost_method": (
        "A conservative UTF-8 preflight plus CONTEXT_CEILING / num_predict=1; "
        "both generated contents are discarded. This path INFORMS a budget "
        "decision. It exists specifically to recover the gap the "
        "conservative byte_estimate cost_method leaves for the current "
        "human message (which can never be pre-calibrated): two real "
        "prompt_eval_count reads (WITH vs WITHOUT the exact text) isolate "
        "its real token cost, mathematically bounded above by "
        "estimate_tokens() for that same text."
    ),
    "preflight_owner": (
        "run_waking_turn() -- invoked only AFTER pass1_result/pass2_result "
        "already reports fits=False from a genuine HARD-only overflow, and "
        "only when the measurement prompt itself passes conservative UTF-8 "
        "admission. Never on the ordinary/fast admission path."
    ),
    "enabled": True,
    "reachability_note": (
        "Multi-turn waking reliability repair (production incident, "
        "2026-09-05): reached only on a would-otherwise-be BUDGET_EXCEEDED "
        "pass1 or pass2 admission, for a safely measurable long human "
        "message. A message whose measurement probe cannot itself be "
        "conservatively admitted still fails closed with zero model calls."
    ),
}

RETAINED_DIALOGUE_COST_RECOVERY_PROBE = {
    "path_id": "retained_dialogue_cost_recovery_probe",
    "file": "llama_anaxi.py",
    "function": "_measure_retained_dialogue_cost",
    "wrapper_call_site": ("llama_anaxi.py", "_measure_retained_dialogue_cost"),
    "model": "gemma4:e4b",
    "budget_profile": "CONTEXT_CEILING / num_predict=1 / SAFETY_MARGIN",
    "cost_method": "byte estimate bounds probe; full prompt_eval_count charged to retained dialogue",
    "preflight_owner": "_measure_retained_dialogue_cost; run_waking_turn failed-composition recovery only",
    "enabled": True,
    "reachability_note": (
        "Only a mandatory retained dialogue floor after ordinary Pass-1/Pass-2 "
        "budget rejection. Original roles retained; one bounded call, no output "
        "persistence, no budget increase, no optional material restored."
    ),
}

WTR0_COLD_RESET_UNLOAD_PROBE = {
    "path_id": "wtr0_cold_reset_unload_probe",
    "file": "wtr0_cold_reset.py",
    "function": "cold_reset_waking_inference_path",
    "wrapper_call_site": ("wtr0_cold_reset.py", "cold_reset_waking_inference_path"),
    "model": "N/A -- whatever model_tag the caller passes (production waking: gemma4:e4b)",
    "budget_profile": "N/A -- not a generation call; keep_alive=0 with an empty messages list, no decode",
    "cost_method": "N/A -- zero prompt/generation tokens by construction (messages=[])",
    "preflight_owner": "N/A -- WTR0 recovery gate itself owns when this runs, never ordinary admission",
    "enabled": True,
    "reachability_note": (
        "THIN-INFERENCE-PROVIDER-BOUNDARY-V0 registry correction: this "
        "call site (ollama_module.chat(model=model_tag, messages=[], "
        "keep_alive=0), the WTR0 mechanical residency-unload actuator) "
        "already existed in production and was discoverable by this same "
        "AST guard; it was missing from this registry, a pre-existing gap "
        "unrelated to this job's own new mlx-serve seam, closed here "
        "rather than left alongside it. Reached only via WTR0's own "
        "recovery path, never ordinary waking admission."
    ),
}

MLXSERVE_PROVIDER_DISPATCH = {
    "path_id": "mlxserve_provider_dispatch",
    "file": "inference_provider.py",
    "function": "_mlxserve_chat",
    "wrapper_call_site": ("inference_provider.py", "_mlxserve_chat"),
    "model": "caller-supplied (production waking's only caller passes gemma4:e4b's MODEL constant or an explicit model)",
    "budget_profile": "N/A -- reuses the caller's own already-composed options/generation_reserve verbatim",
    "cost_method": "N/A -- this module transports a request the caller already budgeted; it adds no cost logic",
    "preflight_owner": "the caller (llama_anaxi.call_llama()/ask_llama_for_json()) -- unchanged from the Ollama path",
    "enabled": True,
    "reachability_note": (
        "THIN-INFERENCE-PROVIDER-BOUNDARY-V0: only reachable when a caller "
        "explicitly resolves provider='mlx-serve' (via the ANAXI_INFERENCE_"
        "PROVIDER environment variable or an explicit argument) -- default "
        "production behavior with no explicit selection remains Ollama, "
        "which keeps calling ollama.chat(...) directly from call_llama()/"
        "ask_llama_for_json() and never reaches this file at all."
    ),
}

SLEEP_PROMPT_MEASUREMENT_PROBE = {
    "path_id": "sleep_prompt_measurement_probe",
    "file": "llama_sleep.py",
    "function": "measure_prompt_tokens",
    "wrapper_call_site": ("llama_sleep.py", "ask_llama_for_json"),
    "model": "llama3.2:3b",
    "budget_profile": "N/A -- a num_predict=1, discarded-output measurement of the EXACT Sleep prompt",
    "cost_method": "provider prompt_eval_count of the exact messages",
    "preflight_owner": "context_budget.admit_by_measurement() via sleep_selection/sleep_transformation",
    "enabled": True,
    "reachability_note": (
        "Second-chance admission only: reached when the byte upper bound rejects a Selection/"
        "Transformation prompt and the production Sleep entrypoint "
        "(the sleep_cycle production-defaults entrypoint) supplied the measurer. Never "
        "generates content; a failed or untrustworthy probe leaves the rejection in force."
    ),
}

ALL_PATHS = [
    CONVERSATION_PASS1,
    CONVERSATION_PASS2,
    REAL_TEXT_COST_RECOVERY_PROBE,
    RETAINED_DIALOGUE_COST_RECOVERY_PROBE,
    TASK_PASS2_TASK_MODE,
    TASK_PASS2_REGEN,
    ARTIFACT_JUDGMENT,
    WSP1_PASS1,
    WSP1_PASS2,
    WSP1_PASS2_VISION,
    ROAMING_STAGE1,
    ROAMING_STAGE2_PUBLIC,
    ROAMING_STAGE2_PRIVATE,
    SLEEP_REM_CONSOLIDATION,
    SLEEP_IDENTITY_REFLECTION,
    WTR0_COLD_RESET_UNLOAD_PROBE,
    SLEEP_PROMPT_MEASUREMENT_PROBE,
    MLXSERVE_PROVIDER_DISPATCH,
    SLEEP_SELECTION,
    SLEEP_TRANSFORMATION,
]

# The (file, function) pairs the bypass guard (test_owc9p4_bypass_
# guard.py) treats as approved, AST-discoverable wrapper call sites --
# derived from this same registry's own wrapper_call_site field (a
# SET: several path rows legitimately share one physical call site) so
# the registry and the guard can never silently drift apart. OWC9-P4A
# section 3 cross-checks this bidirectionally: every entry here must
# still be genuinely discoverable in source, and every chat() call the
# guard discovers in a non-standalone production file must appear here.
WRAPPER_CALL_SITES = {row["wrapper_call_site"] for row in ALL_PATHS}
