"""The one place the production Workspace root is decided.

The Workspace lives next to the code (``anaxi_final/workspace``) and holds the
owner's real journal, public collections and Private Space. Anything that runs
outside the launcher -- above all the test suite and engineering probes -- must
never reach it by accident: a probe that let the subject act through
``WorkspacePaths.production_defaults()`` once appended a fabricated journal
entry and log records to the LIVE Workspace.

``ANAXI_TEST_WORKSPACE_ROOT`` redirects the root. tests' conftest sets it to a
per-session temporary directory, so no test can touch the real Workspace unless
it names one explicitly. The launcher REFUSES to start while it is set, so it
can never silently redirect a real session.
"""
from __future__ import annotations

import os

TEST_WORKSPACE_ROOT_ENV = "ANAXI_TEST_WORKSPACE_ROOT"


def workspace_root() -> str:
    override = os.environ.get(TEST_WORKSPACE_ROOT_ENV)
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace")


OBSIDIAN_VAULT_ROOT_ENV = "ANAXI_OBSIDIAN_WORKSPACE_ROOT"


def default_obsidian_vault_root() -> str:
    """The restored content root of the Windows-era "Clark Kara Other" vault on this Mac (the
    migration payload was restored one level down, so its Archive/Books/Journal/Misc/... live in
    the inner folder).  The single default for every vault consumer, including the public-resource
    ingest's source root."""
    return os.path.join(os.path.expanduser("~"), "Documents", "Clark Kara Other", "Clark Kara Other")


def obsidian_vault_root() -> str:
    """The shared Obsidian vault (the owner's collaborative notes; Clark's artifacts land here too).

    The same isolation as the Workspace: while the test root is redirected, the vault is a
    directory inside that redirected root, so no test or probe can read or write the owner's real
    vault even when the owner's shell exports ANAXI_OBSIDIAN_WORKSPACE_ROOT.  Production resolution:
    the environment variable, else default_obsidian_vault_root()."""
    override = os.environ.get(TEST_WORKSPACE_ROOT_ENV)
    if override:
        return os.path.join(os.path.abspath(override), "obsidian_vault")
    return os.environ.get(OBSIDIAN_VAULT_ROOT_ENV) or default_obsidian_vault_root()


def refuse_if_vault_misaligned(public_source_root) -> None:
    """Called by the launcher: the vault every note/artifact consumer uses (obsidian_vault_root())
    must be the same directory the public-resource ingest reads Books/Pictures/Music from.  Two
    different roots means one of them points at the wrong level of the restored vault -- Clark's
    Journal/*.md artifacts and notes would land beside, not inside, the preserved content -- so a
    real session refuses to start rather than silently split the vault.  A redirected (test) root
    is refused by refuse_if_redirected() instead."""
    if os.environ.get(TEST_WORKSPACE_ROOT_ENV):
        return
    vault = os.path.realpath(obsidian_vault_root())
    public = os.path.realpath(os.fspath(public_source_root))
    if vault != public:
        raise SystemExit(
            f"Obsidian vault root {vault!r} ({OBSIDIAN_VAULT_ROOT_ENV}) is not the public-resource "
            f"source root {public!r}; refusing to start ANAXI against a split vault. "
            "Point both at the restored vault content root and launch again."
        )


def refuse_if_redirected() -> None:
    """Called by the launcher: a production session must use the real Workspace."""
    if os.environ.get(TEST_WORKSPACE_ROOT_ENV):
        raise SystemExit(
            f"{TEST_WORKSPACE_ROOT_ENV} is set; refusing to start ANAXI against a redirected Workspace. "
            "Unset it and launch again."
        )
