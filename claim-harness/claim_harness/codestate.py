"""Optional code-state record: which code the evidence was produced from.

Uses the local ``git`` binary when the project is a git work tree.  Anything
that cannot be established is reported as ``UNAVAILABLE`` with a reason; it
never blocks building a ledger.  No network, no hosting service.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .evidence import sha256_file

ESTABLISHED = "ESTABLISHED"
UNAVAILABLE = "UNAVAILABLE"


def _git(root: Path, *args: str):
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"git not runnable: {exc}"
    if proc.returncode != 0:
        return None, (proc.stderr.strip() or f"git exited {proc.returncode}")
    return proc.stdout, None


def code_state(project_root, evidence_files=()) -> dict:
    """Describe the code under ``project_root`` and hash the given evidence files.

    ``evidence_files`` are paths relative to ``project_root``.
    """
    root = Path(project_root).resolve()
    files = {rel: sha256_file(root / rel) for rel in sorted(set(evidence_files))}
    record = {"project_root": str(root), "evidence_files": files}

    inside, err = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.strip() != "true":
        record.update(status=UNAVAILABLE, reason=f"not a git work tree ({err or 'no'})",
                      head=None, dirty=None)
        return record
    head, err = _git(root, "rev-parse", "HEAD")
    if head is None:
        record.update(status=UNAVAILABLE, reason=f"no commit to identify ({err})", head=None, dirty=None)
        return record
    status, err = _git(root, "status", "--porcelain", "--untracked-files=normal")
    record.update(
        status=ESTABLISHED,
        head=head.strip(),
        dirty=None if status is None else bool(status.strip()),
        reason=None if status is not None else f"dirty state unknown ({err})",
    )
    return record
