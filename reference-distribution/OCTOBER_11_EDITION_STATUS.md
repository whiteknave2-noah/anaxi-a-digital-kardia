# ANAXI reference distribution -- October 11 edition status

**Status: NOT STARTED this session.** This file exists so that absence is
recorded rather than implied. Nothing under `reference-distribution/` above
this file has been touched: the September 24 edition described in
`DISTRIBUTION_STATUS.md`, `DISTRIBUTION_MANIFEST.json` and
`DISTRIBUTION_PROVENANCE.json` remains exactly as it was, and is not claimed
to represent the October 11 architecture.

## What was verified this session

- The private production repository exists at the path given in the build
  instructions and is a git repository on branch `master`.
- Commit `c8615d10f8abc83202aaa27066fc679d34cc0834` exists in that repository,
  is dated 2026-10-11 14:04:13 -0700, and its subject line is "Reject
  superseded staged waking turn replay".
- The private repository's `master` branch has commits after that one
  (for example `d5f0f3d` "Add authenticated Porch resource delivery and owner
  controls" and `db47e73` "Project spoken Pulse contributions through The
  Porch"), so later production work exists on top of the accepted snapshot.
  Anyone building the October 11 edition must build from
  `c8615d10f8abc83202aaa27066fc679d34cc0834` itself, not from the branch tip,
  and must re-verify that no later commit quietly becomes the base.

No file contents from the private repository were copied, and no privacy,
inventory, or test-coverage claims about the October 11 architecture are made
here. Building a second, honestly provenanced edition (source inventory,
privacy-safe code extraction, synthetic fixtures, a reproducible regression
subset, and a privacy/leakage scan comparable in rigor to the September 24
edition's `a3_export.py` pipeline) is a substantial, separate undertaking that
was not attempted in this session; attempting a partial version of it risked
producing exactly the kind of unverified, prose-derived claim this build was
asked not to make.

## What a future pass needs

1. A read-only inventory of the private repository's source tree *at*
   `c8615d10f8abc83202aaa27066fc679d34cc0834`, not at the current branch tip.
2. Explicit, file-by-file selection of what is privacy-safe to publish from
   that commit, run through the same kind of privacy/leakage scan recorded in
   `DISTRIBUTION_PROVENANCE.json` for the September edition.
3. A new, separately versioned directory (not an in-place edit of the
   September 24 tree) with its own `DISTRIBUTION_STATUS.md`,
   `DISTRIBUTION_MANIFEST.json`, and `DISTRIBUTION_PROVENANCE.json` naming the
   October 11 commit as source.
4. A reproducibility pass equivalent to the September edition's: synthetic
   fixtures, an offline regression subset bound to real tests, and an honest
   accounting of what remains production-only or NOT_ESTABLISHED.
