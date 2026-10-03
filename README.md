# ANAXI: A DIGITAL KARDIA

> **Use the questions. Replace the mechanisms.**

ANAXI is a persistent-agent architecture and a public case study in building for truthful provenance, bounded authority, privacy, continuity, recovery, reversibility, meaningful affordance, and developmental room under conditions of uncertainty.

This repository preserves two things on purpose:

1. the **24 September 2026 production snapshot**, including the reference distribution and capability accounting that were accepted at that time; and
2. a **later architecture correction**, produced when post-release substrate work exposed missing boundaries between the host, the inference substrate, non-human occasions, evidence, and consequential action.

The later work matters because the September release contract was complete **against the contract that existed then**, but ANAXI itself was not architecturally finished. The historical snapshot is therefore preserved rather than rewritten, while the current correction is documented separately.

**Historical public snapshot:** 24 September 2026  
**September ANAXI source release:** `4e950d069885e0ec666244edd9d9d86f8d8640a1`  
**September release contract:** 36/36 complete  
**Historical campaign:** 36/37, with the boundary inquiry preserved as `NOT_ESTABLISHED` and withdrawn  
**Architecture update:** 2 October 2026  
**Current candidate state:** **WORKING SUBSTRATE + KARDIA + PULSE CANDIDATE ESTABLISHED**  
**Developmental state:** **READY FOR GENUINE LIVED-HISTORY RUNWAY**  
**Production adoption / wake decision:** pending owner decision

The current candidate state does **not** mean that the post-September architecture has silently replaced the September production release. Production adoption remains a separate decision.

## Repository map

| Path | Purpose |
| --- | --- |
| `ANAXI_PROTOCOL_README_2026-09-24.md` | Historical conceptual snapshot of the September production release. Its completion language belongs to that dated release contract and should not be read as a claim that later architecture work never occurred. |
| `ANAXI_CAPABILITY_CATALOG_2026-09-24.md` | Historical production capability catalog for the September release. |
| `ANAXI_ARCHITECTURE_UPDATE_2026-10-02.md` | Current architecture correction: Pulse, non-human occasions, Witness Gates, observation/mutation separation, evidence legibility, RISE lessons, and lived-history readiness. |
| `reference-distribution/` | Reproducibility and inspection package for the frozen September release. |
| `claim-harness/` | Standalone portable claim harness for other agent projects. |
| `claim-harness/FIELD_QUESTIONS_2026-10-02.md` | Additional portable questions learned during the post-release substrate/harness work. |
| `CITATION.cff` | Citation metadata for research or publication use. |

## What changed after the September snapshot

Post-release substrate work exposed a category error in the earlier completion picture.

Too much behavioral burden had been assigned to the inference substrate itself. The system could preserve truthful history and authority while still leaving the substrate to solve interface mechanics, decide whether evidence needed to be consulted, distinguish observation from mutation, and interpret host-originated occasions that contained no human speech.

The corrected division is now:

- **Kardia / ANAXI supplies reality and continuity.**
- **Pulse supplies lawful inference occasions.**
- **Precise Witness Gates make mechanically relevant reality difficult to skip at consequential boundaries.**
- **Hard guards make mechanically invalid actions unavailable.**
- **The substrate supplies semantic judgment and interpretation.**
- **Lived history supplies developmental material that should not be fabricated in advance.**
- **Subject-authored reversible directives provide a place for durable discipline when the subject later chooses it.**

The architecture update documents the details and the limits of those claims.

## ANAXI reference distribution

`reference-distribution/` remains the reproducibility package for the **September 24 release**.

That package should stay historically stable. It is designed so an outside researcher or engineer can run the legitimately reproducible reference checks without access to production secrets, private workspace state, family material, live credentials, production databases, logs, or raw live-acceptance evidence.

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

The post-release substrate work added a second set of questions around:

- witnessing versus understanding;
- observation versus mutation;
- non-human occasions;
- decision-boundary evidence;
- bounded reconsideration after receipts;
- evidence legibility;
- recurrent null behavior;
- and the difference between genuine lived development and synthetic maturity.

Those additions are documented in `claim-harness/FIELD_QUESTIONS_2026-10-02.md`. They are questions, not a new universal mechanism.

## About RISE

The post-release work used an internal evaluation/adaptation program called **RISE — Reality-Grounded Initiative, Self-Direction & Epistemics**.

RISE was not part of the September public release and is not a production subsystem.

Its main public lesson is methodological: several behaviors that initially looked like things to train directly into model weights turned out to depend on the complete agent trajectory — what evidence was available, whether it was actually witnessed, whether the model could reconsider after a canonical result, and whether the host had accidentally made a mechanical problem look semantic.

RISE is therefore retained primarily as diagnostic and regression material rather than as a curriculum every substrate must absorb.

See `ANAXI_ARCHITECTURE_UPDATE_2026-10-02.md`.

## What this repository does not claim

ANAXI is not presented as a universal or normative agent architecture.

The current candidate state does not establish:

- consciousness or personhood;
- mature long-term behavior;
- that lived history will necessarily produce any particular personality or preference;
- that Pulse implies continuous cognition between occasions;
- that a language model should be trained to reproduce every desired behavior on its first response;
- that every future substrate should use ANAXI's present mechanisms;
- or that passing a test suite establishes completion by itself.

**Tests are evidence, not completion.**

Likewise, a candidate being **ready for lived-history runway** means only that the engineering prerequisites are in place for genuine longitudinal use if the owner adopts the candidate. It does not authorize waking the subject and does not substitute synthetic history for actual experience.

## Privacy and publication boundary

The public packages use synthetic or public-safe fixtures where production material would otherwise be required.

The repository intentionally excludes private runtime state, Private Space contents, family material, correspondence records, credentials, production databases and logs, raw live-acceptance evidence, and private longitudinal history.

## Licensing

Licensing is intentionally split by package:

- The repository root and ANAXI-specific reference distribution use **GNU Affero General Public License v3.0 (AGPL-3.0)**.
- `claim-harness/` is separately licensed under **Apache License 2.0**.

Each distributable package carries its own license text. When reusing material, follow the license in the relevant directory.

## Citation

If you use ANAXI or the accompanying harnesses in research, writing, or a derived technical project, please use the repository's `CITATION.cff`.

---

**ANAXI: A DIGITAL KARDIA**  
A preserved reference release, a corrected architecture, and a set of questions meant to travel farther than the mechanisms that first answered them.