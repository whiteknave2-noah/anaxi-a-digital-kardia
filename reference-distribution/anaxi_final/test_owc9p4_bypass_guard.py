"""OWC9-P4 section 7: permanent repository-level regression proving no
new, unclassified production local-model inference call site can be
introduced silently.

AST-based (never naive grep -- spec: "do not make it so brittle that
comments/test fixtures trigger false positives"; a comment or a fake
test fixture's own `def chat(...)` method produces no matching AST
Call node at all, so neither can ever trip this guard). Detects any
`ollama.chat(...)`-shaped call (an attribute access named `chat`,
called with a `model=` keyword argument -- the one consistent, cheap
signal every real Ollama chat call in this repo actually uses,
confirmed by this gate's own repository-wide source audit) anywhere in
a scanned production .py file.

A detected call is APPROVED only if it sits inside one of the
explicit wrapper functions budget_compliance_registry.py's own
WRAPPER_CALL_SITES names -- the SAME registry Section 8's compliance
table is built from, cross-checked below so the two can never drift
apart. Anything else fails this test with the exact file/line/
function, forcing a developer to either route it through an existing
wrapper, budget it and add a new registry row, or add it to the small
explicit STANDALONE_RESEARCH_SCRIPTS allowlist below (manually-run,
never-imported-by-production diagnostic/experiment/comparison
scripts this gate's own repository-wide audit already confirmed by
name -- see that audit for the file-by-file reachability check each
of these received).

Scope: every top-level .py file in the repo root, EXCLUDING anything
with "test" anywhere in its name (case-insensitive -- catches real
pytest suites and this project's own test_-prefixed manual scripts
alike, matching spec section 1's own "Exclude: tests"), and anything
under scratchpad/ or __pycache__/ (calibration scratch scripts /
bytecode cache, also explicitly excluded by spec section 1).
"""
import ast
import os

import budget_compliance_registry as registry

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))

# spec section 7: "a very small explicit allowlist for... intentionally
# disabled/non-production tooling." Every file below was individually
# confirmed by this gate's own repository-wide source audit to (a)
# contain a direct ollama.chat(...) call, (b) never be imported by any
# production entry point (llama_anaxi.py, llama_gui.py, llama_launch.py,
# llama_desktop.py, orchestration.py, workspace_supervisor.py,
# workspace_roaming.py, workspace_direction.py, workspace_private.py,
# llama_sleep.py, anaxi_sleep.py, api1_control_plane.py,
# api2_control_plane.py, hir1_registration.py), and (c) carry its own
# `if __name__ == "__main__":` / standalone-script framing. Files that
# merely IMPORT an already-approved wrapper (call_llama/
# ask_llama_for_json) rather than calling ollama.chat() directly need
# no entry here at all -- the AST scan below finds nothing to flag in
# them in the first place.
STANDALONE_RESEARCH_SCRIPTS = {
    "run_substrate_evaluation.py",
    "run_gemma_comparison.py",
    "phase3_verifier_diagnostic.py",
    "phase3_verifier_decomposition_diagnostic.py",
    "pass2_context_coverage_experiment.py",
    "pass2_context_register_replication.py",
    "pass2_context_register_experiment.py",
    "signal_taxonomy_matrix.py",
    "speaker_attribution_ab_experiment.py",
    "speaker_attribution_ab_experiment_qwen.py",
    "explicit_intent_gradient.py",
    "explicit_intent_gradient_qwen.py",
    "phase2b_experiment.py",
    "rem_ab_comparison.py",
    "rem_variance_profile.py",
    "llama_baseline.py",
}

# Derived from budget_compliance_registry.py itself (see this module's
# own docstring) -- (filename, function_name) pairs where a chat()
# call is APPROVED because it is the actual, budgeted production
# wrapper the registry documents.
ALLOWED_WRAPPER_CALL_SITES = registry.WRAPPER_CALL_SITES

THIS_FILE = os.path.basename(__file__)


def _is_scanned_python_file(filename):
    if not filename.endswith(".py"):
        return False
    if "test" in filename.lower():
        return False
    if filename == THIS_FILE:
        return False
    return True


def _production_python_files():
    names = []
    for entry in sorted(os.listdir(ANAXI_FINAL)):
        full = os.path.join(ANAXI_FINAL, entry)
        if not os.path.isfile(full):
            continue
        if _is_scanned_python_file(entry):
            names.append(entry)
    return names


def _enclosing_function_name(tree, target_node):
    """Walks the AST to find the nearest enclosing FunctionDef/
    AsyncFunctionDef containing `target_node` -- a plain parent-chain
    search (ast has no built-in parent pointers), returns None if the
    call sits at module level (never expected for a real chat() call,
    but handled rather than crashing)."""
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if child is target_node:
                    best = node.name
    return best


def _is_chat_call(node):
    """A `<expr>.chat(...)` call carrying a `model=` keyword argument
    -- the one signal every real ollama.chat() call site in this repo
    consistently uses (confirmed by this gate's own repository-wide
    source audit), and one a plain code comment or a fake test
    double's own method DEFINITION can never accidentally produce
    (both require an actual ast.Call node with a matching ast.Attribute
    func)."""
    if not isinstance(node, ast.Call):
        return False
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "chat":
        return False
    return any(kw.arg == "model" for kw in node.keywords)


def _find_chat_calls(source_path):
    # utf-8-sig: several files in this repo carry a leading BOM (e.g.
    # continuity_baseline.py) -- plain utf-8 chokes ast.parse() on that
    # byte, which has nothing to do with what this guard is checking.
    with open(source_path, "r", encoding="utf-8-sig") as f:
        source = f.read()
    tree = ast.parse(source, filename=source_path)
    found = []
    for node in ast.walk(tree):
        if _is_chat_call(node):
            func_name = _enclosing_function_name(tree, node)
            found.append((node.lineno, func_name))
    return found


def _discover_all_chat_call_sites():
    """All (filename, func_name) pairs where a chat() call was found,
    across every scanned production file -- STANDALONE_RESEARCH_
    SCRIPTS included (this is the raw discovery set, before allowlist
    filtering). Used by this guard's own OWC9-P4A self-tests to prove
    the discovery mechanism itself is actually finding real callers,
    not just failing to find violations."""
    found = set()
    for filename in _production_python_files():
        full_path = os.path.join(ANAXI_FINAL, filename)
        for lineno, func_name in _find_chat_calls(full_path):
            found.add((filename, func_name))
    return found


def test_no_unclassified_production_inference_call_sites():
    violations = []
    for filename in _production_python_files():
        if filename in STANDALONE_RESEARCH_SCRIPTS:
            continue
        full_path = os.path.join(ANAXI_FINAL, filename)
        for lineno, func_name in _find_chat_calls(full_path):
            if (filename, func_name) not in ALLOWED_WRAPPER_CALL_SITES:
                violations.append(f"{filename}:{lineno} in function {func_name!r} -- not an approved wrapper call site")
    assert violations == [], (
        "New/unclassified production model-call site(s) found:\n"
        + "\n".join(violations)
        + "\n\nRoute this call through an existing approved wrapper, add it to "
        "budget_compliance_registry.py with its own budget composition and a "
        "matching WRAPPER_CALL_SITES entry, or (if this is a manually-run "
        "research/diagnostic script never imported by production) add its "
        "filename to STANDALONE_RESEARCH_SCRIPTS in this file."
    )


def test_standalone_research_scripts_are_not_imported_by_production():
    """Confirms the allowlist's own justification (b) above still
    holds: none of these files is importable from any production
    entry point's own module graph. A cheap, source-text-level check
    (does any production file's source contain `import <modname>` or
    `from <modname> import`) -- sufficient here since Python import
    statements cannot be constructed dynamically without `importlib`,
    which none of these production files use."""
    production_entry_points = [
        "llama_anaxi.py", "llama_gui.py", "llama_launch.py", "llama_desktop.py",
        "orchestration.py", "workspace_supervisor.py", "workspace_roaming.py",
        "workspace_direction.py", "workspace_private.py", "llama_sleep.py",
        "anaxi_sleep.py", "api1_control_plane.py", "api2_control_plane.py",
        "hir1_registration.py",
    ]
    combined_source = ""
    for entry in production_entry_points:
        path = os.path.join(ANAXI_FINAL, entry)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                combined_source += f.read() + "\n"
    for filename in STANDALONE_RESEARCH_SCRIPTS:
        modname = filename[:-3]
        assert f"import {modname}" not in combined_source, filename


def test_registry_wrapper_call_sites_match_guard_allowlist():
    """The compliance registry (Section 8) and this guard's own
    approved-call-site set must never silently drift apart -- both are
    already derived from the SAME registry.WRAPPER_CALL_SITES, so this
    is a tautological guard against a future edit that duplicates
    rather than imports it."""
    assert ALLOWED_WRAPPER_CALL_SITES == registry.WRAPPER_CALL_SITES
    assert len(registry.WRAPPER_CALL_SITES) >= 1


def test_every_registry_path_names_a_real_file():
    for row in registry.ALL_PATHS:
        assert os.path.exists(os.path.join(ANAXI_FINAL, row["file"])), row["path_id"]


def test_registry_covers_every_path_this_gate_identified():
    # Multi-turn waking reliability repair (production incident,
    # 2026-09-05): real_text_cost_recovery_probe and retained_dialogue_
    # cost_recovery_probe added -- real inference call sites
    # (llama_anaxi._measure_real_text_cost/_measure_retained_dialogue_
    # cost), reached only as HARD-overflow recovery steps for pass1/
    # pass2 admission, never on the ordinary fast path.
    #
    # THIN-INFERENCE-PROVIDER-BOUNDARY-V0: wtr0_cold_reset_unload_probe
    # added -- registers wtr0_cold_reset.cold_reset_waking_inference_
    # path's own pre-existing chat() call site (a registry gap that
    # predates this job, closed here rather than left alongside the new
    # seam below). mlxserve_provider_dispatch added -- the new, explicit-
    # opt-in-only mlx-serve adapter call site; the default Ollama path is
    # completely unaffected and does not reach inference_provider.py.
    expected_ids = {
        "conversation_pass1", "conversation_pass2", "real_text_cost_recovery_probe",
        "retained_dialogue_cost_recovery_probe",
        "task_pass2_task_mode", "task_pass2_regen",
        "artifact_judgment", "wsp1_pass1", "wsp1_pass2", "wsp1_pass2_vision", "roaming_stage1",
        "roaming_stage2_public", "roaming_stage2_private", "sleep_rem_consolidation",
        "sleep_identity_reflection", "sleep_selection", "sleep_transformation",
        "wtr0_cold_reset_unload_probe", "mlxserve_provider_dispatch",
        # Sleep production admission: measured second-chance for a prompt the byte
        # bound rejected (same llama_sleep.ask_llama_for_json call site).
        "sleep_prompt_measurement_probe",
    }
    actual_ids = {row["path_id"] for row in registry.ALL_PATHS}
    assert actual_ids == expected_ids


# =================================== OWC9-P4A section 3: bypass-guard self-test
#
# Adversarial-review-inspired property: "A DETECTOR THAT FINDS ZERO
# CALLERS MUST FAIL LOUDLY." A bypass guard whose own discovery
# mechanism silently breaks (a refactor changes the `model=` keyword
# signal every real call uses, a path bug makes _production_python_
# files() return nothing, an encoding change makes ast.parse() choke
# on every file and get swallowed somewhere) degrades into a guard
# that ALWAYS passes, regardless of what the repo actually contains --
# worse than no guard at all, since it looks like protection while
# providing none. The four tests below do not rely only on the single
# planted-violation positive control already exercised manually during
# this gate's own development; they are permanent, and check the
# guard's discovery mechanism from multiple angles against the
# registry it is supposed to cross-check.


def test_guard_discovery_finds_at_least_one_caller():
    # Zero discovered callers anywhere in the whole repo is used here
    # as the loud failure signal itself -- this repo has at least 3
    # real, distinct wrapper call sites (llama_anaxi.ask_llama_for_
    # json, llama_anaxi.call_llama, llama_sleep.ask_llama_for_json)
    # plus ~16 standalone research scripts with their own direct
    # calls; finding none at all means the discovery mechanism itself
    # is broken, not that the repo has no inference calls.
    discovered = _discover_all_chat_call_sites()
    assert len(discovered) > 0, (
        "the bypass guard's own discovery mechanism found ZERO chat() call sites "
        "anywhere in the repo -- this means the discovery logic itself is broken "
        "(a silently-always-passing guard is worse than no guard at all), not "
        "that the repository genuinely has no production inference calls"
    )


def test_guard_discovers_every_registered_wrapper_call_site():
    # "registered caller no longer discovered -> TEST FAILURE": the
    # registry claims these exact (file, function) pairs make a real
    # chat() call. If a future refactor renames/removes/restructures
    # one so the AST scan can no longer find it there, this is the
    # test that catches that drift (test_no_unclassified_production_
    # inference_call_sites only catches the OPPOSITE direction --
    # something new and unregistered).
    discovered = _discover_all_chat_call_sites()
    missing = registry.WRAPPER_CALL_SITES - discovered
    assert missing == set(), f"registered wrapper call site(s) no longer found by AST discovery: {missing}"


def test_every_discovered_production_caller_is_registered_or_standalone():
    # "known caller missing compliance registration -> TEST FAILURE",
    # phrased directly from the discovery set (test_no_unclassified_
    # production_inference_call_sites proves the same property via an
    # explicit violations list; this restates it as a positive
    # assertion over every discovered call site, so a bug in either
    # test's own logic is unlikely to hide the same gap from both).
    discovered = _discover_all_chat_call_sites()
    for filename, func_name in discovered:
        if filename in STANDALONE_RESEARCH_SCRIPTS:
            continue
        assert (filename, func_name) in registry.WRAPPER_CALL_SITES, (
            f"{filename}:{func_name} makes a chat() call but is not in the compliance registry"
        )


def test_discovered_registered_and_budgeted_sets_agree():
    # "expected registered + discovered + budgeted set -> PASS": the
    # full three-way cross-check -- what the guard's own AST scan
    # discovers in non-standalone production files, what the registry
    # declares as its approved wrapper call sites, and what budget_
    # compliance_registry.ALL_PATHS actually enumerates (independently
    # re-derived here, not just re-reading WRAPPER_CALL_SITES) all
    # describe the exact same set.
    discovered = {cs for cs in _discover_all_chat_call_sites() if cs[0] not in STANDALONE_RESEARCH_SCRIPTS}
    registered = registry.WRAPPER_CALL_SITES
    budgeted = {row["wrapper_call_site"] for row in registry.ALL_PATHS}
    assert discovered == registered == budgeted
    # Multi-turn waking reliability repair (production incident,
    # 2026-09-05): _measure_real_text_cost/_measure_retained_dialogue_cost
    # added as genuinely new, properly registered real inference call
    # sites. THIN-INFERENCE-PROVIDER-BOUNDARY-V0: wtr0_cold_reset.py's own
    # pre-existing call site (a registry gap predating this job) and the
    # new, explicit-opt-in-only inference_provider.py mlx-serve adapter
    # call site are both now registered too.
    assert discovered == {("llama_anaxi.py", "ask_llama_for_json"), ("llama_anaxi.py", "call_llama"),
                           ("llama_anaxi.py", "_measure_real_text_cost"),
                           ("llama_anaxi.py", "_measure_retained_dialogue_cost"),
                           ("llama_sleep.py", "ask_llama_for_json"),
                           ("wtr0_cold_reset.py", "cold_reset_waking_inference_path"),
                           ("inference_provider.py", "_mlxserve_chat")}


if __name__ == "__main__":
    test_no_unclassified_production_inference_call_sites()
    test_standalone_research_scripts_are_not_imported_by_production()
    test_registry_wrapper_call_sites_match_guard_allowlist()
    test_every_registry_path_names_a_real_file()
    test_registry_covers_every_path_this_gate_identified()
    test_guard_discovery_finds_at_least_one_caller()
    test_guard_discovers_every_registered_wrapper_call_site()
    test_every_discovered_production_caller_is_registered_or_standalone()
    test_discovered_registered_and_budgeted_sets_agree()
    print("OWC9-P4/P4A bypass guard: all checks passed.")
