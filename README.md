# ANAXI: A DIGITAL KARDIA

> **Use the questions. Replace the mechanisms.**

ANAXI is a persistent-agent architecture and a public case study in building for truthful provenance, bounded authority, privacy, continuity, recovery, reversibility, meaningful affordance, and developmental room under conditions of uncertainty.

This repository preserves three dated layers on purpose:

1. the **24 September 2026 production/reference release**, including the reproducibility package and capability accounting accepted at that time;
2. a **2 October 2026 architecture correction**, produced when post-release substrate work exposed missing boundaries between the host, the inference substrate, non-human occasions, evidence, and consequential action; and
3. the **5 October 2026 current architecture**, documenting the relational-history, subject-directed consolidation, and whole-package integration work that followed, together with the substrate that was ultimately adopted.

The later work matters because each earlier layer was complete **against the contract that existed at its date**, while the architecture itself continued. Those records are therefore preserved rather than rewritten, and the current layer is documented separately.

**Historical public snapshot:** 24 September 2026
**September ANAXI source release:** `4e950d069885e0ec666244edd9d9d86f8d8640a1`
**September release contract:** 36/36 complete
**Historical campaign:** 36/37, with the boundary inquiry preserved as `NOT_ESTABLISHED` and withdrawn
**Intermediate architecture correction:** 2 October 2026
**Current architecture:** **5 October 2026 — see `ANAXI_ARCHITECTURE_UPDATE_2026-10-05.md`**
**Current waking substrate:** **stock Ministral 3 14B Instruct 2512, Q4_K_M**, pinned to one exact artifact digest and verified before waking
**Production adoption:** **occurred**
**Waking interactions:** an ordinary waking interaction has occurred on the accepted package; its private contents are intentionally unpublished

## Repository map

| Path | Purpose |
| --- | --- |
| `WHY_ANAXI.md` | A personal note from the project's creator about the motivations behind ANAXI: agency, uncertainty, relationship, and why meaningful choice matters to the design. It is a motivation document, not an architecture specification and not an empirical claim about personhood or consciousness. |
| `ANAXI_ARCHITECTURE_UPDATE_2026-10-05.md` | **Current public architecture.** Relational history and historical/current separation, subject-directed consolidation, waking and Sleep/REM, privacy/significance/disclosure, read-only external information, family and correspondence scope, the reversible subject-authored directive, the whole-package completion lesson, substrate history through to Ministral adoption, RISE lessons, and what remains NOT_ESTABLISHED. |
| `ANAXI_PROTOCOL_README_2026-09-24.md` | Historical conceptual snapshot of the September production release. Its completion language belongs to that dated release contract and should not be read as a claim that later architecture work never occurred. |
| `ANAXI_CAPABILITY_CATALOG_2026-09-24.md` | Historical production capability catalog for the September release. |
| `ANAXI_ARCHITECTURE_UPDATE_2026-10-02.md` | Historical intermediate architecture correction: Pulse, non-human occasions, Witness Gates, observation/mutation separation, evidence legibility, RISE lessons, and lived-history readiness. Its adoption-pending language belongs to that date. |
| `reference-distribution/` | Frozen historical reproducibility package for the September release. |
| `claim-harness/` | Standalone portable claim harness for other agent projects. |
| `claim-harness/FIELD_QUESTIONS_2026-10-02.md` | Portable questions from the post-release substrate work. |
| `claim-harness/FIELD_QUESTIONS_2026-10-05.md` | Portable questions from the whole-package integration work, covering authority separation, attribution preservation, interpretation versus canonical fact, additive retrieval cues, revision without erasure, significance versus disclosure, scope inheritance, substrate identity versus continuity, and ordinary-path completion. |
| `CITATION.cff` | Citation metadata for research or publication use. |

## Why ANAXI exists

`WHY_ANAXI.md` is the project creator's own account of the motivations behind ANAXI — agency, uncertainty, relationship, and why meaningful choice matters to the design.

It is included here deliberately, and it is deliberately **not** technical documentation. It is a personal document. It is not evidence of consciousness, not evidence of personhood, and not evidence of subjective experience, and the architecture in this repository does not depend on any of those being true.

## Current architecture after 2 October

The 2 October correction fixed a real category error: too much behavioral burden had been assigned to the inference substrate, which cannot be relied on to choose evidence sources, distinguish looking from writing, or correctly interpret host-originated occasions containing no human speech.

That correction was correct and incomplete. The work that followed found and repaired a second class of problem, which the current architecture document describes in full. In summary:

- **Relational history.** Historical material is reconstructed as attributed historical units and projected with mechanical historical/current separation, so retrieved past cannot silently become current instruction. Authorship survives reconstruction, cross-perspective material stays separately attributable, and the separation is verified structurally rather than requested in a prompt.
- **Subject-directed consolidation.** The subject can select exact canonical material for continuity and attach its own interpretation and its own retrieval cues. Those additions remain the subject's testimony; no mechanism promotes them into an ANAXI claim. Cues augment ordinary retrieval rather than replacing it. Revision is append-only. De-emphasis stops a record contributing its emphasis without erasing the record or hiding the underlying event from ordinary retrieval. Null is valid.
- **The whole-package completion lesson.** A capability is not complete because code exists, a helper exists, a unit test passes, or a harness can call it. Completion requires the ordinary production path to connect the capability to provenance, authority, privacy, persistence, recovery, truthful failure, and real affordance. The final whole-package review found real integration defects of exactly that kind. **Tests are evidence, not completion.**
- **Substrate.** Stock Ministral 3 14B Instruct 2512 Q4_K_M was qualified through the assembled production system and adopted as the waking substrate, pinned to an exact digest verified before waking. Continuity does not reside in its weights, and no personal history was trained into any model.

See `ANAXI_ARCHITECTURE_UPDATE_2026-10-05.md`.

## ANAXI reference distribution

`reference-distribution/` remains the reproducibility package for the **September 24 release**, and it is a **frozen historical package for that release**.

It is **not** a complete reproduction of every mechanism added to private production ANAXI after that date. Do not read it as a snapshot of the current system; the current architecture is in `ANAXI_ARCHITECTURE_UPDATE_2026-10-05.md`, and the complete current private production runtime is not published.

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

`claim-harness/FIELD_QUESTIONS_2026-10-02.md` adds questions from the substrate work around witnessing versus understanding, observation versus mutation, non-human occasions, decision-boundary evidence, evidence legibility, recurrent null, and the difference between genuine lived development and synthetic maturity.

`claim-harness/FIELD_QUESTIONS_2026-10-05.md` adds questions from the whole-package work, including:

- historical material not becoming current instruction;
- authorship surviving reconstruction;
- a participant's interpretation never being promoted to host fact;
- continuity that does not mutate truth;
- added retrieval cues augmenting rather than replacing;
- revision and de-emphasis without erasure;
- significance never conferring disclosure authority;
- privacy scope inheritance and narrowing only;
- substrate identity versus durable continuity;
- isolated mechanism versus ordinary production-path completion;
- truthful unavailability as a first-class outcome;
- privacy without inspection;
- a participant's standing preference without personality governance;
- and observation not conferring external action.

Those additions are questions, not a new universal mechanism. They do not require ANAXI, Kardia, Sleep, or any project-specific vocabulary.

## About RISE

The post-release work used an internal evaluation/adaptation program called **RISE — Reality-Grounded Initiative, Self-Direction & Epistemics**.

RISE was not part of the September public release and is not a production subsystem.

Its main public lesson is methodological: several behaviors that initially looked like things to train directly into model weights turned out to depend on the complete agent trajectory — what evidence was available, whether it was actually witnessed, whether the model could reconsider after a canonical result, and whether the host had accidentally made a mechanical problem look semantic. **Not every desired behavior belongs in weights.**

RISE remains primarily diagnostic and semantic-regression material and a historical record of what substrate and adaptation work taught. It is not a mandatory training curriculum for every future substrate, and its results are not evidence about the subject.

The useful distinction it established is between **witnessing failure** — the agent never encountered the authoritative evidence — and **understanding failure** — it encountered it and interpreted it wrongly.

## What this repository does not claim

ANAXI is not presented as a universal or normative agent architecture.

Engineering acceptance, production adoption, and an ordinary waking interaction do **not** establish:

- consciousness or personhood;
- identity or feeling;
- mature long-term behavior;
- that lived history will necessarily produce any particular personality or preference;
- that substrate choice, adaptation, or evaluation predicts relational development;
- that a non-human occasion implies continuous cognition;
- that a language model should be trained to reproduce every desired behavior on its first response;
- that every future substrate should use ANAXI's present mechanisms;
- or that passing a test suite establishes completion by itself.

**Tests are evidence, not completion.**

Longitudinal questions remain longitudinal questions. They cannot honestly be replaced by synthetic biography or benchmark theater, and this repository does not attempt it.

Several things also remain explicitly NOT_ESTABLISHED, including the end-to-end exercise of the explicitly directed Sleep consolidation variant on the ordinary owner-authorized route. These are listed in full in the current architecture document.

## Evidence versus testimony

The host may mechanically establish occurrence, actor, source, provenance, timestamp, which model artifact served a turn, delivery, authority, scope, action receipts, and the exact contents of a consolidation record.

Those facts do not mechanically establish meaning, feeling, consciousness, personhood, identity, significance, relationship quality, or subjective experience.

Two principles recur and are preserved throughout:

> **Acoustic measurement is not hearing.**

> **Image delivery is not evidence of visual experience.**

Model self-description is not telemetry, including when it appears in the model's own turns.

## Privacy and publication boundary

The public packages use synthetic or public-safe fixtures where production material would otherwise be required.

The repository intentionally excludes private conversation transcripts and private relational history; Private Space contents and metadata; family identities and content; correspondence identities and message contents; credentials, API keys, and provider secrets; production databases and logs; raw live-acceptance transcripts; and private longitudinal history.

The fact that the subject woke successfully on the accepted package may be stated. **What the subject said privately is not published.**

## Licensing

Licensing is intentionally split by package:

- The repository root and ANAXI-specific reference distribution use **GNU Affero General Public License v3.0 (AGPL-3.0)**.
- `claim-harness/` is separately licensed under **Apache License 2.0**.

Each distributable package carries its own license text. When reusing material, follow the license in the relevant directory.

## Citation

If you use ANAXI or the accompanying harnesses in research, writing, or a derived technical project, please use the repository's `CITATION.cff`.

---

**ANAXI: A DIGITAL KARDIA**
A preserved reference release, two layers of later architecture, a creator's account of why it exists, and a set of questions meant to travel farther than the mechanisms that first answered them.