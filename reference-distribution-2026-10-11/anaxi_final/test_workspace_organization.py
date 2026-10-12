"""CAP2-D acceptance tests for workspace_organization.py -- the
non-destructive public organizational overlay. Zero model/network
calls (this module makes none, structurally). Every test uses a fresh
temp workspace root."""
import json
import os
import sys
import tempfile
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import workspace_capability as wc
import workspace_organization as wo

TEST_ROOT = tempfile.mkdtemp(prefix="worg_test_")


def fresh_paths(name):
    root = os.path.join(TEST_ROOT, name)
    paths = wc.WorkspacePaths(root=root)
    paths.ensure_exists()
    return paths


def write_fixture(paths, resource_class, filename, content=b"fixture"):
    d = paths.dir_for(resource_class)
    os.makedirs(d, exist_ok=True)
    full = os.path.join(d, filename)
    with open(full, "wb") as f:
        f.write(content)
    return full


def test_create_list_inspect_roundtrip():
    paths = fresh_paths("create_list_inspect")
    created, failure = wo.create_container(paths, "collection", "Favorites")
    assert failure is None
    assert created["container_type"] == "collection"
    assert created["name"] == "Favorites"
    assert created["resource_references"] == []

    listing, failure = wo.list_containers(paths)
    assert failure is None
    assert listing["returned_count"] == 1
    assert listing["entries"][0]["container_id"] == created["container_id"]
    assert listing["entries"][0]["reference_count"] == 0

    inspected, failure = wo.inspect_container(paths, created["container_id"])
    assert failure is None
    assert inspected == created


def test_create_rejects_unknown_container_type():
    paths = fresh_paths("bad_type")
    result, failure = wo.create_container(paths, "shrine", "x")
    assert result is None
    assert "container_type" in failure["rationale"]


def test_create_rejects_empty_name():
    paths = fresh_paths("empty_name")
    result, failure = wo.create_container(paths, "collection", "   ")
    assert result is None


def test_rename_container():
    paths = fresh_paths("rename")
    created, _ = wo.create_container(paths, "shelf", "Old Name")
    renamed, failure = wo.rename_container(paths, created["container_id"], "New Name")
    assert failure is None
    assert renamed["name"] == "New Name"
    inspected, _ = wo.inspect_container(paths, created["container_id"])
    assert inspected["name"] == "New Name"


def test_rename_nonexistent_fails_closed():
    paths = fresh_paths("rename_missing")
    result, failure = wo.rename_container(paths, "org-doesnotexist", "New Name")
    assert result is None
    assert failure["rationale"] == "Container not found."


def test_add_reference_to_existing_library_file():
    paths = fresh_paths("add_ref_ok")
    write_fixture(paths, wc.LIBRARY, "book.txt", b"once upon a time")
    created, _ = wo.create_container(paths, "shelf", "To Read")
    updated, failure = wo.add_reference(paths, created["container_id"], wc.LIBRARY, "book.txt")
    assert failure is None
    assert len(updated["resource_references"]) == 1
    assert updated["resource_references"][0]["resource_class"] == wc.LIBRARY
    assert updated["resource_references"][0]["relative_path"] == "book.txt"


def test_add_reference_rejects_nonexistent_resource():
    paths = fresh_paths("add_ref_missing")
    created, _ = wo.create_container(paths, "shelf", "To Read")
    result, failure = wo.add_reference(paths, created["container_id"], wc.LIBRARY, "nope.txt")
    assert result is None
    assert "does not exist" in failure["rationale"]
    inspected, _ = wo.inspect_container(paths, created["container_id"])
    assert inspected["resource_references"] == []


def test_add_reference_rejects_path_traversal():
    paths = fresh_paths("add_ref_traversal")
    created, _ = wo.create_container(paths, "shelf", "To Read")
    result, failure = wo.add_reference(paths, created["container_id"], wc.LIBRARY, "../../etc/passwd")
    assert result is None


def test_add_reference_rejects_unknown_resource_class():
    paths = fresh_paths("add_ref_unknown_class")
    created, _ = wo.create_container(paths, "shelf", "To Read")
    result, failure = wo.add_reference(paths, created["container_id"], "videos", "clip.mp4")
    assert result is None


def test_add_reference_is_idempotent_no_duplicate():
    paths = fresh_paths("add_ref_dup")
    write_fixture(paths, wc.MUSIC, "song.mp3")
    created, _ = wo.create_container(paths, "playlist", "Chill")
    wo.add_reference(paths, created["container_id"], wc.MUSIC, "song.mp3")
    updated, _ = wo.add_reference(paths, created["container_id"], wc.MUSIC, "song.mp3")
    assert len(updated["resource_references"]) == 1


def test_remove_reference_does_not_delete_source_file():
    paths = fresh_paths("remove_ref")
    full = write_fixture(paths, wc.PHOTOGRAPHS, "pic.jpg")
    created, _ = wo.create_container(paths, "album", "Trip")
    wo.add_reference(paths, created["container_id"], wc.PHOTOGRAPHS, "pic.jpg")
    updated, failure = wo.remove_reference(paths, created["container_id"], wc.PHOTOGRAPHS, "pic.jpg")
    assert failure is None
    assert updated["resource_references"] == []
    assert os.path.isfile(full)  # source untouched


def test_remove_reference_not_found_fails_closed():
    paths = fresh_paths("remove_ref_missing")
    created, _ = wo.create_container(paths, "album", "Trip")
    result, failure = wo.remove_reference(paths, created["container_id"], wc.PHOTOGRAPHS, "nope.jpg")
    assert result is None
    assert failure["rationale"] == "Reference not found in this container."


def test_delete_container_removes_only_the_container_never_the_source():
    paths = fresh_paths("delete_container")
    full = write_fixture(paths, wc.LIBRARY, "book.txt")
    created, _ = wo.create_container(paths, "shelf", "Shelf")
    wo.add_reference(paths, created["container_id"], wc.LIBRARY, "book.txt")
    result, failure = wo.delete_container(paths, created["container_id"])
    assert failure is None
    assert result["status"] == "deleted"
    assert wo.inspect_container(paths, created["container_id"]) == (None, {"rationale": "Container not found."})
    assert os.path.isfile(full)  # source resource must survive


def test_delete_nonexistent_container_fails_closed():
    paths = fresh_paths("delete_missing")
    result, failure = wo.delete_container(paths, "org-doesnotexist")
    assert result is None
    assert failure["rationale"] == "Container not found."


def test_list_containers_bounded_and_deterministic():
    paths = fresh_paths("list_many")
    ids = []
    for i in range(5):
        created, _ = wo.create_container(paths, "collection", f"Set {i}")
        ids.append(created["container_id"])
    listing, _ = wo.list_containers(paths)
    assert listing["total_count"] == 5
    assert listing["returned_count"] == 5
    assert listing["truncated"] is False


def test_action_log_records_every_attempt_never_raw_container_content():
    paths = fresh_paths("org_log")
    created, _ = wo.create_container(paths, "collection", "Logged")
    wo.rename_container(paths, created["container_id"], "Renamed")
    wo.rename_container(paths, "org-nope", "x")
    records = wo.query_action_log(paths)
    actions = [r["action"] for r in records]
    assert wo.CREATE in actions
    assert wo.RENAME in actions
    denied = [r for r in records if r["result"] == "denied"]
    assert len(denied) == 1


def test_no_model_or_network_imports():
    with open(os.path.join(ANAXI_FINAL, "workspace_organization.py"), encoding="utf-8") as f:
        source = f.read()
    assert "ollama" not in source.lower()
    assert "requests" not in source and "urllib" not in source and "socket" not in source
    import ast
    tree = ast.parse(source)
    module_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_names.append(node.module)
    assert set(module_names) == {"datetime", "json", "os", "uuid", "workspace_capability"}, module_names


ALL_TESTS = [
    test_create_list_inspect_roundtrip,
    test_create_rejects_unknown_container_type,
    test_create_rejects_empty_name,
    test_rename_container,
    test_rename_nonexistent_fails_closed,
    test_add_reference_to_existing_library_file,
    test_add_reference_rejects_nonexistent_resource,
    test_add_reference_rejects_path_traversal,
    test_add_reference_rejects_unknown_resource_class,
    test_add_reference_is_idempotent_no_duplicate,
    test_remove_reference_does_not_delete_source_file,
    test_remove_reference_not_found_fails_closed,
    test_delete_container_removes_only_the_container_never_the_source,
    test_delete_nonexistent_container_fails_closed,
    test_list_containers_bounded_and_deterministic,
    test_action_log_records_every_attempt_never_raw_container_content,
    test_no_model_or_network_imports,
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
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
