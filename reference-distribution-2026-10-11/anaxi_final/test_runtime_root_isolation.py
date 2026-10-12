"""Regression: no test or engineering probe may reach the LIVE Workspace.

Incident (2026-09-18): a real-model probe run through pytest let the subject
act via ``WorkspacePaths.production_defaults()`` -- the real Workspace -- and a
fabricated journal entry (fixture session id, the subject's real actor id) plus
log records landed in the owner's live data. A companion scratch script
appended synthetic records to the live signal log. The redirect below makes the
same probe harmless by construction, and the launcher refuses a redirected
root.
"""
import os

import pytest

import runtime_roots
import workspace_capability as wc
import workspace_private
import public_resource_ingest

LIVE_WORKSPACE = os.path.join(os.path.dirname(os.path.abspath(runtime_roots.__file__)), "workspace")


def test_every_production_default_is_redirected_away_from_the_live_workspace_under_pytest():
    assert os.environ.get(runtime_roots.TEST_WORKSPACE_ROOT_ENV)
    for root in (
        wc.WorkspacePaths.production_defaults().root,
        workspace_private.PrivatePaths.production_defaults().root,
        str(public_resource_ingest.default_workspace_root()),
    ):
        assert os.path.commonpath([os.path.abspath(root), LIVE_WORKSPACE]) != LIVE_WORKSPACE, root


def test_a_subject_journal_append_through_production_defaults_cannot_reach_the_live_journal():
    paths = wc.WorkspacePaths.production_defaults()
    paths.ensure_exists()
    result, failure = wc.append_journal_entry(paths, "actor-synthetic", "isolation probe")
    assert failure is None
    assert not os.path.abspath(paths.journal_dir).startswith(LIVE_WORKSPACE)
    assert os.path.exists(result and os.path.join(paths.journal_dir, result["entry_id"] + ".json"))


def test_without_the_redirect_the_root_is_the_live_workspace_and_the_launcher_refuses_a_redirect(monkeypatch):
    monkeypatch.delenv(runtime_roots.TEST_WORKSPACE_ROOT_ENV)
    assert runtime_roots.workspace_root() == LIVE_WORKSPACE
    assert wc.WorkspacePaths.production_defaults().root == LIVE_WORKSPACE
    monkeypatch.setenv(runtime_roots.TEST_WORKSPACE_ROOT_ENV, "/tmp/anywhere")
    with pytest.raises(SystemExit):
        runtime_roots.refuse_if_redirected()
