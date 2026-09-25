import shutil
import subprocess

import pytest

from claim_harness.codestate import code_state


def test_not_a_git_tree_is_unavailable_not_an_error(tmp_path):
    (tmp_path / "t.py").write_text("x = 1\n")
    state = code_state(tmp_path, ["t.py", "gone.py"])
    assert state["status"] == "UNAVAILABLE" and state["head"] is None and state["dirty"] is None
    assert len(state["evidence_files"]["t.py"]) == 64
    assert state["evidence_files"]["gone.py"] is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git binary not installed")
def test_git_head_and_dirty_state(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (tmp_path / "t.py").write_text("x = 1\n")
    git("add", "t.py")
    git("commit", "-q", "-m", "c")
    clean = code_state(tmp_path, ["t.py"])
    assert clean["status"] == "ESTABLISHED" and len(clean["head"]) == 40 and clean["dirty"] is False
    (tmp_path / "t.py").write_text("x = 2\n")
    dirty = code_state(tmp_path, ["t.py"])
    assert dirty["dirty"] is True and dirty["head"] == clean["head"]
    assert dirty["evidence_files"]["t.py"] != clean["evidence_files"]["t.py"]
