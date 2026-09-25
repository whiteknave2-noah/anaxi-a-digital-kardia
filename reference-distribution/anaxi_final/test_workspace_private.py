"""WSP3-S1 acceptance tests. Zero real model/Ollama calls (this module
makes none at all, structurally). Every test uses a fresh temp private
root -- the real production anaxi_final/workspace/private/ path is
never touched, never created, never listed.
"""
import ast
import json
import os
import shutil
import sys
import tempfile
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import workspace_private as wp
import workspace_capability as wc

TEST_ROOT = tempfile.mkdtemp(prefix="wsp3s1_test_")


def fresh_private_paths(name):
    root = os.path.join(TEST_ROOT, name)
    paths = wp.PrivatePaths(root=root)
    paths.ensure_exists()
    return paths


def _module_ast_imports(filename):
    with open(os.path.join(ANAXI_FINAL, filename), encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names, source


# --------------------------------------------------------- A: root/containment


def test_private_root_created_and_resolved():
    paths = fresh_private_paths("root_basic")
    assert os.path.isdir(paths.root)
    real = wp.resolve_private_path(paths, "note.txt")
    assert real == os.path.realpath(os.path.join(paths.root, "note.txt"))


def test_production_defaults_nested_under_workspace_private_not_hardcoded():
    # WSP2-P3-P1 test-hygiene fix: this test verifies the PATH FORMULA
    # PrivatePaths.production_defaults() computes -- it must never
    # depend on whether that path currently exists on disk in this
    # developer's own real, ongoing production usage (WSP2-L1/L2's own
    # real overnight roaming activity has since made that path exist,
    # which is expected and correct, not a defect). The removed
    # assertion here (`assert not os.path.exists(defaults.root)`) was
    # never actually testing PrivatePaths' own behavior -- it was a
    # one-time, point-in-time sanity check from the original WSP3-S1
    # implementation gate ("did I accidentally create this directory
    # myself, right now"), never meant to be a permanent invariant.
    # This test does NOT inspect, stat, list, or otherwise observe any
    # real production private-space content, timestamps, or use
    # pattern -- only the deterministic string PrivatePaths computes.
    import runtime_roots  # formula checked with the test-only redirect removed
    saved = os.environ.pop(runtime_roots.TEST_WORKSPACE_ROOT_ENV, None)
    try:
        defaults = wp.PrivatePaths.production_defaults()
    finally:
        if saved is not None:
            os.environ[runtime_roots.TEST_WORKSPACE_ROOT_ENV] = saved
    assert defaults.root == os.path.join(ANAXI_FINAL, "workspace", "private")
    assert "Clark Kara Other" not in defaults.root
    # Functional check, not a prose substring scan (the module's own
    # docstring legitimately explains "never Path.home()" in prose) --
    # the module's exact AST import set (asserted elsewhere) excludes
    # pathlib entirely, so Path.home() cannot even be called here.
    names, source = _module_ast_imports("workspace_private.py")
    assert "pathlib" not in names
    assert "from pathlib import Path" not in source


def test_traversal_rejected():
    paths = fresh_private_paths("traversal")
    for bad in ("../outside.txt", "..\\outside.txt", "a/../../outside.txt"):
        try:
            wp.resolve_private_path(paths, bad)
            assert False, f"expected PathEscapeError for {bad!r}"
        except wp.PathEscapeError:
            pass


def test_absolute_path_rejected():
    paths = fresh_private_paths("absolute")
    abs_path = os.path.join(TEST_ROOT, "elsewhere.txt")
    try:
        wp.resolve_private_path(paths, abs_path)
        assert False, "expected PathEscapeError"
    except wp.PathEscapeError:
        pass


def test_rename_destination_escape_rejected():
    paths = fresh_private_paths("rename_escape")
    validated, failure = wp.validate_private_action({
        "action": "write", "relative_path": "a.txt", "content": "x", "destination_relative_path": "",
    })
    assert failure is None
    result, failure = wp.execute_private_action(paths, validated)
    assert failure is None

    rename_action, _ = wp.validate_private_action({
        "action": "rename", "relative_path": "a.txt", "content": "",
        "destination_relative_path": "../escaped.txt",
    })
    result2, failure2 = wp.execute_private_action(paths, rename_action)
    assert result2 is None
    assert failure2 == {"failure_class": wp.PrivateFailure.PATH_ESCAPE}
    # The original file is untouched -- nothing was created outside the root.
    assert not os.path.exists(os.path.join(TEST_ROOT, "escaped.txt"))
    assert os.path.isfile(os.path.join(paths.root, "a.txt"))


def test_symlink_escape_rejected_where_detectable():
    paths = fresh_private_paths("symlink_escape")
    outside_dir = os.path.join(TEST_ROOT, "symlink_outside_secret")
    os.makedirs(outside_dir, exist_ok=True)
    with open(os.path.join(outside_dir, "secret.txt"), "w", encoding="utf-8") as f:
        f.write("outside secret")
    link_path = os.path.join(paths.root, "link_to_outside")
    try:
        os.symlink(outside_dir, link_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        print("SKIP: symlink creation not permitted in this environment (Windows without privilege) -- "
              "Windows symlink/reparse-point escape is a KNOWN, EXPLICITLY CARRIED-FORWARD limitation "
              "(spec section 6), not solved in this gate, exactly mirroring workspace_capability.py's "
              "own equivalent limitation.")
        return
    try:
        wp.resolve_private_path(paths, "link_to_outside/secret.txt")
        assert False, "expected PathEscapeError -- symlink target resolves outside the private root"
    except wp.PathEscapeError:
        pass


# ------------------------------------------------------------- B: read/write


def test_write_then_read_exact_content():
    paths = fresh_private_paths("write_read")
    write_action, _ = wp.validate_private_action({
        "action": "write", "relative_path": "note.txt", "content": "Private thought.", "destination_relative_path": "",
    })
    result, failure = wp.execute_private_action(paths, write_action)
    assert failure is None
    assert result == {"action": "write", "status": "written"}

    read_action, _ = wp.validate_private_action({
        "action": "read", "relative_path": "note.txt", "content": "", "destination_relative_path": "",
    })
    read_result, read_failure = wp.execute_private_action(paths, read_action)
    assert read_failure is None
    assert read_result["content"] == "Private thought."
    assert read_result["has_more"] is False


def test_append_creates_and_extends():
    paths = fresh_private_paths("append")
    append1, _ = wp.validate_private_action({
        "action": "append", "relative_path": "log.md", "content": "first ", "destination_relative_path": "",
    })
    r1, f1 = wp.execute_private_action(paths, append1)
    assert f1 is None
    append2, _ = wp.validate_private_action({
        "action": "append", "relative_path": "log.md", "content": "second", "destination_relative_path": "",
    })
    r2, f2 = wp.execute_private_action(paths, append2)
    assert f2 is None
    read_action, _ = wp.validate_private_action({
        "action": "read", "relative_path": "log.md", "content": "", "destination_relative_path": "",
    })
    read_result, _ = wp.execute_private_action(paths, read_action)
    assert read_result["content"] == "first second"


def test_write_overwrites_full_contents():
    paths = fresh_private_paths("overwrite")
    for text in ("version one", "version two, much shorter overwrite"):
        write_action, _ = wp.validate_private_action({
            "action": "write", "relative_path": "doc.txt", "content": text, "destination_relative_path": "",
        })
        wp.execute_private_action(paths, write_action)
    read_action, _ = wp.validate_private_action({
        "action": "read", "relative_path": "doc.txt", "content": "", "destination_relative_path": "",
    })
    read_result, _ = wp.execute_private_action(paths, read_action)
    assert read_result["content"] == "version two, much shorter overwrite"


def test_rename_within_root_succeeds():
    paths = fresh_private_paths("rename_ok")
    write_action, _ = wp.validate_private_action({
        "action": "write", "relative_path": "old.txt", "content": "moving", "destination_relative_path": "",
    })
    wp.execute_private_action(paths, write_action)
    rename_action, _ = wp.validate_private_action({
        "action": "rename", "relative_path": "old.txt", "content": "", "destination_relative_path": "new.txt",
    })
    result, failure = wp.execute_private_action(paths, rename_action)
    assert failure is None
    assert not os.path.exists(os.path.join(paths.root, "old.txt"))
    assert os.path.isfile(os.path.join(paths.root, "new.txt"))


def test_delete_removes_item():
    paths = fresh_private_paths("delete")
    write_action, _ = wp.validate_private_action({
        "action": "write", "relative_path": "gone.txt", "content": "temporary", "destination_relative_path": "",
    })
    wp.execute_private_action(paths, write_action)
    delete_action, _ = wp.validate_private_action({
        "action": "delete", "relative_path": "gone.txt", "content": "", "destination_relative_path": "",
    })
    result, failure = wp.execute_private_action(paths, delete_action)
    assert failure is None
    assert not os.path.exists(os.path.join(paths.root, "gone.txt"))

    delete_again, _ = wp.validate_private_action({
        "action": "delete", "relative_path": "gone.txt", "content": "", "destination_relative_path": "",
    })
    result2, failure2 = wp.execute_private_action(paths, delete_again)
    assert result2 is None
    assert failure2 == {"failure_class": wp.PrivateFailure.NOT_FOUND}


def test_no_revision_or_backup_history_created():
    paths = fresh_private_paths("no_backup")
    for text in ("draft one", "draft two", "final draft"):
        write_action, _ = wp.validate_private_action({
            "action": "write", "relative_path": "essay.txt", "content": text, "destination_relative_path": "",
        })
        wp.execute_private_action(paths, write_action)
    entries = os.listdir(paths.root)
    assert entries == ["essay.txt"]  # no .bak, no .essay.txt.1, no hidden temp file left behind


def test_unsupported_extension_rejected_for_content_operations():
    paths = fresh_private_paths("unsupported_ext")
    write_action, _ = wp.validate_private_action({
        "action": "write", "relative_path": "note.exe", "content": "x", "destination_relative_path": "",
    })
    result, failure = wp.execute_private_action(paths, write_action)
    assert result is None
    assert failure == {"failure_class": wp.PrivateFailure.UNSUPPORTED_EXTENSION}
    assert os.listdir(paths.root) == []


# ------------------------------------------------------------------ C: listing


def test_bounded_list_returns_sorted_entries():
    paths = fresh_private_paths("listing")
    for name in ("b.txt", "a.md", "c.txt"):
        write_action, _ = wp.validate_private_action({
            "action": "write", "relative_path": name, "content": "x", "destination_relative_path": "",
        })
        wp.execute_private_action(paths, write_action)
    list_action, _ = wp.validate_private_action({
        "action": "list", "relative_path": "", "content": "", "destination_relative_path": "",
    })
    result, failure = wp.execute_private_action(paths, list_action)
    assert failure is None
    assert result == {"action": "list", "entries": ["a.md", "b.txt", "c.txt"]}


# --------------------------------------------------------- D: schema/validation


def test_validate_private_action_rejects_malformed_and_leakage():
    validated, failure = wp.validate_private_action("not json {{{")
    assert failure == wp.PrivateFailure.MALFORMED_ACTION
    validated, failure = wp.validate_private_action({"action": "read"})
    assert failure == wp.PrivateFailure.MALFORMED_ACTION
    validated, failure = wp.validate_private_action({
        "action": "read", "relative_path": "", "content": "", "destination_relative_path": "", "extra": "leak",
    })
    assert failure == wp.PrivateFailure.PROTOCOL_LEAKAGE
    validated, failure = wp.validate_private_action({
        "action": "fly_to_the_moon", "relative_path": "", "content": "", "destination_relative_path": "",
    })
    assert failure == wp.PrivateFailure.INVALID_ACTION


# --------------------------------------------------- E: transient observation


def test_observation_is_separate_field_from_roaming_observation():
    wp.reset_private_state()
    assert wp.get_private_state() == {"last_private_observation": None}
    wp.set_last_observation({"action": "list", "entries": ["a.txt"]})
    assert wp.get_private_state()["last_private_observation"] == {"action": "list", "entries": ["a.txt"]}
    # Structurally a completely different global than workspace_roaming's
    # own state holder -- proven by import isolation below, and by name:
    # this module has no "last_workspace_observation" key anywhere.
    assert "last_workspace_observation" not in wp.get_private_state()


def test_observation_replaced_not_accumulated():
    wp.reset_private_state()
    wp.set_last_observation({"action": "read", "content": "FIRST"})
    wp.set_last_observation({"action": "read", "content": "SECOND"})
    obs = wp.get_private_state()["last_private_observation"]
    assert obs == {"action": "read", "content": "SECOND"}
    assert "FIRST" not in json.dumps(wp.get_private_state())


def test_reset_clears_observation():
    wp.set_last_observation({"action": "read", "content": "to be cleared"})
    wp.reset_private_state()
    assert wp.get_private_state()["last_private_observation"] is None


def test_render_last_private_observation_pure_no_summary():
    assert wp.render_last_private_observation(None) == "Previous private observation: none"
    rendered = wp.render_last_private_observation({"action": "list", "entries": ["a.txt"]})
    assert "a.txt" in rendered
    assert "never shown to Alex" in rendered  # explicit host-grounded framing, never a bare content dump


# -------------------------------------------------------------- F: no trace


def test_no_durable_trace_file_written_by_this_module():
    # workspace_private.py never writes anything to disk except the
    # actual private files the caller explicitly asked to write --
    # no *_trace.jsonl, no action log, no history file of any kind.
    with open(os.path.join(ANAXI_FINAL, "workspace_private.py"), encoding="utf-8") as f:
        source = f.read()
    # Narrow, functional checks -- not a bare "history" substring scan,
    # since the module's own docstring/comments legitimately explain
    # (in prose) that no revision history is created.
    for forbidden in ("trace.jsonl", "_log_action(", "action_log", ".jsonl"):
        assert forbidden not in source, forbidden
    paths = fresh_private_paths("no_trace")
    write_action, _ = wp.validate_private_action({
        "action": "write", "relative_path": "secret.txt", "content": "SENSITIVE_MARKER", "destination_relative_path": "",
    })
    wp.execute_private_action(paths, write_action)
    # Only the one file the caller asked for exists -- nothing else.
    assert os.listdir(paths.root) == ["secret.txt"]


# ---------------------------------------------------- G: memory/retrieval/sleep


def test_workspace_private_imports_nothing_beyond_minimal_stdlib():
    # OWC9-P4 section 3: context_budget is the ONE deliberate, blessed
    # exception -- it is a generic, dependency-free, content-free
    # arithmetic authority (byte counts and a fixed budget constant
    # only; it never reads, logs, or exposes private content, and
    # imports nothing of its own beyond the stdlib either). Adding it
    # here budgets Stage-2 private's own prompt through the SAME
    # authority every other pathway already uses (spec: "do not invent
    # a new cost architecture"), without importing any other
    # data-carrying Anaxi subsystem -- the isolation this test actually
    # protects (no workspace_roaming/workspace_direction/conversation_
    # direction/orchestration coupling) is unchanged.
    names, source = _module_ast_imports("workspace_private.py")
    assert names.issubset({"json", "os", "uuid", "dataclasses", "context_budget", "runtime_roots"})
    assert "ollama" not in source.lower()


def test_hippocampus_sources_never_import_workspace_private():
    for filename in ("hippocampus_store.py", "hippocampus_retrieval.py"):
        names, source = _module_ast_imports(filename)
        assert "workspace_private" not in names
        assert "workspace_capability" not in names
        assert "import workspace_private" not in source


def test_dialogue_window_never_imports_workspace_private():
    names, source = _module_ast_imports("session_dialogue_window.py")
    assert "workspace_private" not in names
    assert "import workspace_private" not in source


def test_canonical_journal_never_imports_workspace_private():
    # clark_journal.py's own "workspace_root" concept is the separate,
    # pre-existing OBSIDIAN_WORKSPACE_ROOT (Path.home()/.../"Clark Kara
    # Other") -- a different directory entirely from anaxi_final/
    # workspace/private/, and write_journal_entry() only ever writes
    # ONE new caller-named file; it never enumerates or reads existing
    # directory contents, so it has no path to discover private material
    # even in principle.
    names, source = _module_ast_imports("clark_journal.py")
    assert "workspace_private" not in names
    assert "workspace_capability" not in names
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        llama_anaxi_source = f.read()
    assert "OBSIDIAN_WORKSPACE_ROOT = runtime_roots.obsidian_vault_root()" in llama_anaxi_source
    with open(os.path.join(ANAXI_FINAL, "runtime_roots.py"), encoding="utf-8") as f:
        roots_source = f.read()
    assert 'OBSIDIAN_VAULT_ROOT_ENV = "ANAXI_OBSIDIAN_WORKSPACE_ROOT"' in roots_source
    assert 'str(Path.home() / "OneDrive" / "Desktop" / "Clark Kara Other")' in roots_source


def test_conversation_direction_trace_never_imports_workspace_private():
    names, source = _module_ast_imports("conversation_direction_trace.py")
    assert "workspace_private" not in names


def test_ordinary_waking_pathway_never_imports_private_workspace_module():
    # Final integration deliberately makes the ordinary public Workspace
    # capability reachable from waking. The absolute boundary is that it
    # never imports the separate Private Space module.
    names, source = _module_ast_imports("llama_anaxi.py")
    assert "workspace_private" not in names
    assert "import workspace_private" not in source


def test_sleep_reflection_sources_never_import_workspace_private():
    for filename in ("anaxi_sleep.py", "llama_sleep.py"):
        names, source = _module_ast_imports(filename)
        assert "workspace_private" not in names
        assert "workspace_capability" not in names
        assert "import workspace_private" not in source


def test_gui_imports_workspace_private_only_for_root_wiring_no_content_rendering():
    # WSP3-P1 intentionally wires workspace_private.PrivatePaths.
    # production_defaults() into the GUI's roaming-authorization call
    # site -- this import is now expected. What must remain absent is
    # any CONTENT-facing usage: no private state read, no observation
    # rendering, no failure-detail rendering, no direct action calls.
    names, source = _module_ast_imports("llama_gui.py")
    assert "workspace_private" in names
    assert "import workspace_private" in source
    for forbidden in (
        "get_private_state", "last_private_observation", "render_last_private_observation",
        "PrivateFailure", "execute_private_action", "validate_private_action",
        "build_private_action_messages", "reset_private_state",
    ):
        assert forbidden not in source, forbidden
    # The only production-facing use of the module is the one root-path
    # construction handed to start_roaming_worker().
    assert source.count("workspace_private.") == 1
    assert "workspace_private.PrivatePaths.production_defaults()" in source


# --------------------------------------------------------- H: ordinary isolation


def test_ordinary_library_list_cannot_discover_private_files():
    # Simulates the real on-disk relationship: workspace/private/ nested
    # as a sibling directory alongside library/music/photographs/journal
    # inside the SAME workspace root -- proves list_contents(LIBRARY)
    # only ever lists the library/ subdirectory, never private/.
    root = os.path.join(TEST_ROOT, "isolation_root")
    ordinary_paths = wc.WorkspacePaths(root=root)
    ordinary_paths.ensure_exists()
    private_paths = wp.PrivatePaths(root=os.path.join(root, "private"))
    private_paths.ensure_exists()
    with open(os.path.join(private_paths.root, "secret.txt"), "w", encoding="utf-8") as f:
        f.write("private content")
    with open(os.path.join(ordinary_paths.library_dir, "public_book.txt"), "w", encoding="utf-8") as f:
        f.write("public content")

    result, failure = wc.list_contents(ordinary_paths, wc.LIBRARY)
    assert failure is None
    assert result["entries"] == ["public_book.txt"]
    assert "secret.txt" not in result["entries"]

    journal_result, jfailure = wc.list_journal_entries(ordinary_paths)
    assert jfailure is None
    assert "secret.txt" not in journal_result["entries"]

    for resource_class in (wc.MUSIC, wc.PHOTOGRAPHS):
        rc_result, rc_failure = wc.list_contents(ordinary_paths, resource_class)
        assert rc_failure is None
        assert "secret.txt" not in rc_result["entries"]


# ==================================== WSP2-MA2: Stage-2 structural schema


def test_ma2_schema_shape_object_with_exact_required_keys():
    schema = wp.STAGE2_PRIVATE_ACTION_SCHEMA
    assert schema["type"] == "object"
    assert set(schema["required"]) == wp.RAW_PRIVATE_ACTION_ALLOWED_FIELDS
    assert set(schema["properties"].keys()) == wp.RAW_PRIVATE_ACTION_ALLOWED_FIELDS


def test_ma2_schema_all_values_typed_string():
    schema = wp.STAGE2_PRIVATE_ACTION_SCHEMA
    for field, spec in schema["properties"].items():
        assert spec == {"type": "string"}, field


def test_ma2_schema_forbids_additional_properties():
    assert wp.STAGE2_PRIVATE_ACTION_SCHEMA["additionalProperties"] is False


def test_ma2_schema_no_semantic_policy_encoded():
    # Section 5: no enum/pattern/resource-legality/path-containment
    # semantics belong in the schema -- only mechanical object shape.
    schema = wp.STAGE2_PRIVATE_ACTION_SCHEMA
    for spec in schema["properties"].values():
        assert set(spec.keys()) == {"type"}  # no enum, no pattern, no format, no minLength


def test_ma2_schema_built_from_same_constant_as_validator_never_drifts():
    # The schema and the validator's own required-fields check share
    # ONE source of truth (RAW_PRIVATE_ACTION_ALLOWED_FIELDS) -- they
    # cannot independently drift apart.
    assert set(wp.STAGE2_PRIVATE_ACTION_SCHEMA["required"]) == wp.RAW_PRIVATE_ACTION_ALLOWED_FIELDS


def test_ma2_host_validator_still_authoritative_legal_object():
    validated, failure = wp.validate_private_action(
        {"action": "list", "relative_path": "", "content": "", "destination_relative_path": ""}
    )
    assert failure is None
    assert validated == {"action": "list", "relative_path": "", "content": "", "destination_relative_path": ""}


def test_ma2_host_validator_still_authoritative_semantically_illegal_object():
    # Structurally legal under the schema (all 4 string keys present),
    # but semantically illegal per the validator's own enum check.
    validated, failure = wp.validate_private_action(
        {"action": "not_a_real_action", "relative_path": "", "content": "", "destination_relative_path": ""}
    )
    assert failure == wp.PrivateFailure.INVALID_ACTION


def test_ma2_no_normalization_source_audit():
    with open(os.path.join(ANAXI_FINAL, "workspace_private.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("setdefault(", ".get(\"action\", ", "or \"\"", "coerce"):
        assert forbidden not in source


ALL_TESTS = [
    test_private_root_created_and_resolved,
    test_production_defaults_nested_under_workspace_private_not_hardcoded,
    test_traversal_rejected,
    test_absolute_path_rejected,
    test_rename_destination_escape_rejected,
    test_symlink_escape_rejected_where_detectable,
    test_write_then_read_exact_content,
    test_append_creates_and_extends,
    test_write_overwrites_full_contents,
    test_rename_within_root_succeeds,
    test_delete_removes_item,
    test_no_revision_or_backup_history_created,
    test_unsupported_extension_rejected_for_content_operations,
    test_bounded_list_returns_sorted_entries,
    test_validate_private_action_rejects_malformed_and_leakage,
    test_observation_is_separate_field_from_roaming_observation,
    test_observation_replaced_not_accumulated,
    test_reset_clears_observation,
    test_render_last_private_observation_pure_no_summary,
    test_no_durable_trace_file_written_by_this_module,
    test_workspace_private_imports_nothing_beyond_minimal_stdlib,
    test_hippocampus_sources_never_import_workspace_private,
    test_dialogue_window_never_imports_workspace_private,
    test_canonical_journal_never_imports_workspace_private,
    test_conversation_direction_trace_never_imports_workspace_private,
    test_ordinary_waking_pathway_never_imports_private_workspace_module,
    test_sleep_reflection_sources_never_import_workspace_private,
    test_gui_imports_workspace_private_only_for_root_wiring_no_content_rendering,
    test_ordinary_library_list_cannot_discover_private_files,
    # WSP2-MA2
    test_ma2_schema_shape_object_with_exact_required_keys,
    test_ma2_schema_all_values_typed_string,
    test_ma2_schema_forbids_additional_properties,
    test_ma2_schema_no_semantic_policy_encoded,
    test_ma2_schema_built_from_same_constant_as_validator_never_drifts,
    test_ma2_host_validator_still_authoritative_legal_object,
    test_ma2_host_validator_still_authoritative_semantically_illegal_object,
    test_ma2_no_normalization_source_audit,
]


def main():
    passed, failed = 0, 0
    failures = []
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            tb = traceback.format_exc()
            failures.append((t.__name__, str(exc), tb))
            print(f"FAIL {t.__name__}: {exc}")
    print()
    print(f"TOTAL={len(ALL_TESTS)} PASSED={passed} FAILED={failed}")
    if failures:
        print()
        for name, msg, tb in failures:
            print(f"--- {name} ---")
            print(tb)
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
