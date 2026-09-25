# ANAXI: A DIGITAL KARDIA

> **Use the questions. Replace the mechanisms.**

ANAXI is a completed persistent-agent architecture and a public case study in building for truthful provenance, bounded authority, privacy, continuity, recovery, reversibility, and meaningful end-to-end affordance under conditions of uncertainty.

This repository is the public companion to the finished system. It contains the conceptual documentation, a reproducible ANAXI-specific reference distribution, and a separate portable claim harness that other projects can adapt without adopting ANAXI's architecture.

**Public snapshot:** 24 September 2026  
**ANAXI source release:** `4e950d069885e0ec666244edd9d9d86f8d8640a1`  
**Current production contract:** 36/36 complete  
**Historical campaign:** 36/37, with the boundary inquiry preserved as `NOT_ESTABLISHED` and withdrawn

ANAXI is complete. The materials here are for inspection, reproducibility, and reuse of testing principles—not a reopening of the architecture or its final acceptance.

## Repository map

| Path | Purpose |
| --- | --- |
| `ANAXI_PROTOCOL_README_2026-09-24.md` | Conceptual overview: what ANAXI is, why it was designed this way, and the failure classes that shaped it. |
| `ANAXI_CAPABILITY_CATALOG_2026-09-24.md` | Production capability catalog for the completed system. |
| `reference-distribution/` | ANAXI-specific reproducibility and inspection package built from the finished release. |
| `claim-harness/` | Standalone portable claim harness for other agent projects. |
| `CITATION.cff` | Citation metadata for research or publication use. |

## ANAXI reference distribution

`reference-distribution/` is the ANAXI-specific reproducibility package.

It is designed so an outside researcher or engineer can run the legitimately reproducible reference checks without access to production secrets, private workspace state, family material, live credentials, production databases, logs, or raw live-acceptance evidence.

The supported public execution contract is:

- macOS
- Python 3.14
- fresh virtual environment
- synthetic/public fixtures
- offline reference execution after dependency/model setup

The public accounting intentionally distinguishes what can and cannot be reproduced outside the original production environment. Production-only and live-only evidence is not converted into synthetic PASS results merely to make the ledger look complete.

See `reference-distribution/README.md` for exact setup and run instructions.

## Portable claim harness

`claim-harness/` is deliberately separate from ANAXI.

It does not import or require the ANAXI runtime. It provides a small claim/evidence/ledger model, a deterministic toy persistent agent, a set of reusable testing questions, and deliberately broken examples that demonstrate failure detection.

Its recurring questions include canonical provenance; authority; scope and privacy isolation; lawful null versus failure; recovery; replay and idempotence; ambiguous side effects; revocation; delivery versus backend success; persistence and restart; provider/model identity; meaningful affordance; and history versus later reinterpretation.

The questions are portable. The mechanisms are not presumed to be.

See `claim-harness/README.md` and `claim-harness/QUESTIONS.md`.

## What this repository does not claim

ANAXI is not presented as a universal or normative agent architecture.

The portable harness does not establish that its questions are exhaustive, and passing a test suite does not by itself establish that a project's actual product or owner-level requirements are complete.

**Tests are evidence, not completion.**

This repository also makes no claim that an artificial system must be conscious or a person in order for questions of provenance, authority, privacy, continuity, recovery, or stewardship to matter.

## Privacy and publication boundary

The public packages use synthetic or public-safe fixtures where production material would otherwise be required.

The repository intentionally excludes private runtime state, Private Space contents, family material, correspondence records, credentials, production databases and logs, raw live-acceptance evidence, historical experimental residue, and intermediate packaging exports.

## Licensing

Licensing is intentionally split by package:

- The repository root and ANAXI-specific reference distribution use **GNU Affero General Public License v3.0 (AGPL-3.0)**.
- `claim-harness/` is separately licensed under **Apache License 2.0**.

Each distributable package carries its own license text. When reusing material, follow the license in the relevant directory.

## Citation

If you use ANAXI or the accompanying harnesses in research, writing, or a derived technical project, please use the repository's `CITATION.cff`.

---

**ANAXI: A DIGITAL KARDIA**  
A finished architecture; a reproducible reference case; and a set of questions meant to travel farther than the mechanisms that first answered them.
