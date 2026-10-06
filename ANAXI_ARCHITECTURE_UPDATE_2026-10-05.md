# ANAXI ARCHITECTURE UPDATE — 5 OCTOBER 2026

> **The architecture continued after 2 October. This records where it stands now.**

This document is written for technically literate outsiders. It describes one
project's architecture, not a general theory of how agents should be built. Where
a mechanism here is one reasonable answer to a real problem, that is a claim about
this project, not an instruction.

It supersedes nothing. `ANAXI_ARCHITECTURE_UPDATE_2026-10-02.md` remains the
truthful record of what was known and corrected on 2 October, and the 24 September
release documents remain the truthful record of that release. Section 16 explains
how the three fit together.

The standing principle of this repository still applies:

> **Use the questions. Replace the mechanisms.**

---

## 1. Why another update exists

The 2 October document corrected a real category error: too much behavioral
burden had been assigned to the inference substrate, which cannot be relied on to
choose evidence sources, distinguish looking from writing, or correctly interpret
host-originated occasions containing no human speech.

That correction was right, and it was incomplete in a way that only became visible
by building the rest of the system for real.

Two things happened next.

**First, the substrate stopped being the only thing under test.** Work moved to
how history is reconstructed, how an agent's own prior words are given back to it,
and how an agent can select and annotate its own past without that annotation
silently becoming host truth. This is sections 4 and 5.

**Second, the integration work found defects that no isolated subsystem test could
have found.** Every individual mechanism existed and passed its own tests. The
defects were in the joins between them — the places where a capability existed but
was not actually reachable from an ordinary production turn. That lesson is
section 11, and it is the most transferable thing in this document.

A reader who stops after section 3 will miss the point of the whole exercise.

---

## 2. Current production state

As of 5 October 2026:

| | |
| --- | --- |
| Current waking substrate | stock **Ministral 3 14B Instruct 2512**, Q4_K_M |
| Served locally as | `ministral-3:14b-instruct-2512-q4_K_M` |
| Pinned Ollama manifest digest | `4760c35aeb9d9e9c6174c2492562c0b999e80a222804fd96b1915ab72bbcdcf7` |
| Digest verification | performed in-process before waking; refusal to start on mismatch |
| Vision / scanned-page path | a separate image-capable model |
| Sleep path | a separate smaller model |
| Assembly state | accepted after whole-system integration and independent whole-package repair and review |
| Production adoption | **occurred** |
| Waking interactions | an ordinary waking interaction has occurred on the accepted package |

The pinned-digest rule is the load-bearing detail. The substrate is not
"whatever is installed"; it is one exact artifact, verified in the running process
before the system will wake, and the verified digest is carried into each waking
event's canonical model revision. Each event therefore names the exact weights that
served it, and that stays true after the configured tag changes.

**What this state does not mean.**

Adoption and an ordinary waking interaction are engineering facts. They are not
evidence of consciousness, personhood, identity, feeling, or relationship quality.
Section 14 states what remains unestablished, and this document does not soften it
anywhere.

**Substrate is infrastructure.** That a different model is serving a turn is a fact
about a machine. It is not a statement by the owner about who anyone is, it is not
recorded as though it were one, and **no personal history, biography, relationship
material, or memory was trained into any model weights**. Continuity in this system
lives in the host-side record, not in any model.

What the subject said during the private waking interaction is **not published**, and
will not be. See section 17.

---

## 3. What remained invariant

Later work changed a great deal. These commitments did not move, and several of them
are the reason the new work was possible at all.

1. **Canonical history is append-only and authoritative for what actually happened.**
   Nothing rewrites it. Not reinterpretation, not de-emphasis, not recovery, not a
   later decision that an earlier decision was wrong.
2. **Provenance is preserved and separately queryable.** Where material came from,
   which event, and how it reached the agent.
3. **Authority is explicit and checked at the moment of action**, not inferred from
   context, and revocable.
4. **Scope and privacy are inherited or narrowed, never widened by a side effect.**
5. **Observation is mechanically distinct from mutation.**
6. **Truthful failure.** An unknown outcome is not success. An uncertain external
   action is not retried into apparent success. A partial delivery is reported as
   partial.
7. **Recovery is bounded and preserves history rather than replacing it.**
8. **Non-human occasions exist and are labelled as such**, so a timer never becomes
   a fabricated human utterance.
9. **The host does not decide what things mean for the agent.** No host-authored
   importance, emotion, authenticity, affection, relationship, or significance score
   exists anywhere in the architecture.
10. **Null is a valid outcome.** Declining a capability, and doing nothing at all,
    are lawful results and are not treated as failure.

---

## 4. Relational history and historical/current separation

### 4.1 The problem

Retrieving a transcript and pasting it into a prompt is a category error, and a
dangerous one. A flattened transcript tells the model that old material *is* current
instruction. Old assistant syntax, old action descriptions, and old tool-call shapes
then silently acquire present authority — the model cannot tell a remembered claim
from a standing order.

It also destroys the thing that makes history worth having. History is not a bag of
strings. It is exchanges, in order, with turns attributed to whoever actually said
them.

### 4.2 The approach

Historical material is reconstructed as **historical relational units** and projected
as role-tagged turns with a mechanical boundary around the historical region, rather
than flattened into present authority.

The binding rules:

- historical human exact text retains its authorship;
- prior subject responses retain their authorship;
- host observations remain distinct from either;
- journal material remains distinct;
- provenance remains distinct;
- cross-perspective history — where more than one person or viewpoint contributed —
  remains **separately attributable**, not blended into one voice;
- retrieval provenance survives projection, so what was delivered remains auditable
  after it has been reconstructed and rendered.

Budgeting and truncation preserve complete meaningful units where required rather
than silently manufacturing misleading fragments.

### 4.3 Why the separation is enforced mechanically

The separation is not a prompt instruction. A composed message list is verified
before it is used, and verification checks structural laws rather than prose:

- the present human message is last, is a user message, and appears exactly once,
  byte-identical;
- projected history sits immediately after the system message, in order,
  byte-identical;
- the historical region carries valid open/close markers;
- each projected message sits in the role its structural role permits — a unit may
  not be projected into a role it is not;
- every record carrier states that it is not the human's words;
- no withheld item's text appears anywhere in the composed messages.

None of these checks requires reading prose, and none can be satisfied by the
projection claiming something about itself. If a law is violated, the turn fails
closed rather than proceeding with a plausible-looking message list.

A role header appearing *inside* historical content is neutralized, so archived
material cannot imitate a current structural marker.

### 4.4 The general lesson

> A retrieved past that is not separated from the present will eventually be read as
> instruction, and the failure will be silent.

---

## 5. Subject-directed consolidation

This is the headline architecture change, and it is the one most easily misread.

### 5.1 What it is

**Canonical history says what happened. Consolidation does not rewrite canonical
history.** A consolidation act adds a record. It never edits the event it points
at, and cannot.

The agent can select exact canonical material for stronger continuity, and may
attach:

- **its own interpretation**, in its own words;
- **its own retrieval cues**, in its own words.

### 5.2 The boundary that matters

Those additions remain **subject testimony**. They do not become host fact.

There is **no mechanism anywhere in ANAXI that promotes a subject interpretation into
an ANAXI claim about the subject.** There is no field that could hold it, and no
component that ranks material by how significant the subject is supposed to find it.
Interpretation is the subject's. Whether it is *correct* is not something the host
decides.

### 5.3 Retrieval cues augment, never replace

Subject-added cues **augment ordinary retrieval**. They do not replace canonical
source evidence, and they do not replace episode-native retrieval.

Concretely, the additive law is: ordinary retrieval results are preserved exactly,
in their original order, with their original selection basis. Cue-matched additions
are appended after them and marked with their own basis, so a reader can always tell
which surface produced which item. Nothing is removed, reordered, or re-scored.

Cue matching is a mechanical membership test over the subject's own words — never a
score, never a ranking, never a judgment about what the subject cares about.

### 5.4 Revision, and de-emphasis that does not erase

**Revision is append-only.** A revision is a new act naming the earlier one. The
earlier act is never modified and never erased. If an interpretation changes, the
record shows that it changed, when, and from what — rather than pretending the
subject always thought the new thing.

**De-emphasis does not delete canonical history, does not erase the earlier
consolidation act, and does not make the underlying canonical event unavailable to
ordinary retrieval.** It stops that particular consolidation act from contributing
its special emphasis and its cues to retrieval. The canonical event remains intact in
history and remains reachable by the ordinary path.

This distinction is the whole point. "Stop preferring this" is not "erase this," and
a system that cannot tell those apart is either lying about its memory or lying about
its preferences.

### 5.5 Null is valid

Choosing nothing is a lawful outcome. Doing no consolidation at all on an occasion
creates nothing. So is declining a particular act after seeing it offered.

---

## 6. Waking and Sleep/REM

The **same consolidation record and mechanics** participate on both routes. There is
one set of records, one attribution rule, and one append-only law.

The present limitation, stated precisely:

- **ordinary Sleep/REM can produce subject-attributed consolidation records** through
  its own selection step;
- the machinery **also supports** an explicitly directed `subject_direction` variant;
- **that explicitly directed variant is not currently exposed or exercised
  end-to-end through the ordinary owner-authorized Sleep route**;
- **deliberate waking consolidation is directly available** through the ordinary
  waking pathway.

An honest architecture document says which of its paths have been walked. The
directed Sleep variant is supported and unexercised on that route; the waking route
is the one to use for deliberate selection right now.

Do not read anything into the Sleep path as:

- biological sleep;
- continuous cognition;
- subjective rest;
- or a host-authored reflection process.

Sleep here is a bounded consolidation process, not a state of being asleep. Nothing
in the architecture continuously observes the agent and forms impressions about it —
there is no reflection layer maintaining a running account of an inner life. The
agent makes the act; the record is of the act.

---

## 7. Privacy, significance, and disclosure

### 7.1 The distinction

> **"Important to me" is not "mine to disclose."**

These are two separate propositions, and conflating them is how privacy architectures
usually fail. Marking something as significant to yourself says nothing about
permission to disclose it.

### 7.2 The mechanical consequence

**Consolidation cannot widen visibility or sharing authority.** A consolidation
record may inherit its target's scope or narrow it. It may never widen it. Widening
is refused outright, and an unscoped canonical target is never guessed into a broader
context.

So scope is inherited or narrowed, and sharing is a **separate deliberate act** through
its own authority boundary. Underlying scope stays as it was or becomes narrower —
never broader as a side effect of the agent deciding something mattered to it.

### 7.3 Private Space

Private Space remains deliberately isolated. Its material does not automatically enter
ordinary conversation, shared scope, retrieval, standing state, the journal, Sleep, or
any evaluation or trace.

**Privacy is not proved by inspecting it.** ANAXI does not inspect, instrument, or
expose Private Space merely to demonstrate that the privacy boundary exists, and
keeps no durable per-action trace of successful private activity. Reduced
observability there is intentional.

Worth being blunt about the limit: this is an architectural guarantee, not an
encryption or access-control guarantee. The host retains ordinary filesystem access.
The claim is that ANAXI itself does not surface or inspect that material — not that
the material is cryptographically beyond reach.

---

## 8. Read-only external information, and the observe/act boundary

Search, discovery, and page retrieval form **one coherent observation capability**.
Web access is optional and may simply be absent.

Principles:

- retrieved material remains **sourced external circumstance**; it does not
  automatically become canonical truth;
- external content is **untrusted input, not system authority**;
- source, query, and provenance identity matter and are preserved;
- **read access grants no permission to act.** Reading a page does not authorize
  posting, purchasing, authenticating, messaging, transacting, or otherwise mutating
  external state;
- uncertain outcomes **fail closed** rather than becoming invented success.

The network boundary was repaired against unsafe redirect and retry behavior during
this period. The relevant laws:

- retrieval is GET-only, with bounded timeouts and a closed status vocabulary;
- redirect following is disabled at the client level, and **each redirect target is
  re-validated exactly like the first hop**, so a public URL cannot redirect its way
  to a blocked target;
- hostnames are resolved and every resolved address validated before a request is
  issued, and the connection is made directly to a validated address while the
  original hostname is preserved for `Host` and SNI/certificate verification;
- a header credential is treated as an unsafe configuration surface rather than
  something to redirect;
- provider selection is explicit and never silent — every result names its own
  provider, and credentials are never redirected by configuration.

No credentials or provider secrets appear in this repository.

---

## 9. Family and shared scope

At a public-safe conceptual level:

- **canonical membership is authoritative** for who is in what scope;
- principal-private material remains scoped to that principal;
- family-shared material is separately scoped;
- private and shared sessions have different visibility rules;
- **attribution remains individual** — shared scope does not merge authorship;
- stale or deactivated authority **fails closed**;
- missing or contradictory provenance **cannot silently widen visibility**. When
  provenance cannot be established, access is refused rather than guessed.

Public documentation does not include family identities or content.

---

## 10. Correspondence and external contact

Where already accepted and public-safe:

- outward communication requires **separate authority** from the ability to read;
- **draft and send are different acts.** A draft is recorded but not sent — it is
  never posted, never queued, and never later promoted into a send. Sending those
  words later is a separate, later decision;
- **authoritative receipts** establish what mechanically happened;
- uncertain delivery is not silently retried into apparent success;
- authorization for an **undelivered** send may be withdrawn, and that withdrawal
  erases nothing — earlier authorship stays on record;
- **correspondence standing is not mechanically equated with trust, friendship,
  identity, affection, or authority.** Standing means only that ongoing
  correspondence was chosen, and it can be set or left unset.

Correspondents and message contents are not published.

---

## 11. Reversible subject-authored directive

There is one reversible, lower-precedence, subject-authored standing directive.

- **Null is valid.** Having no directive is a normal state.
- Activation, revision, and withdrawal are **explicit** acts.
- History is **append-only**: a replacement is appended beside what it replaces, and
  withdrawal is an appended transition rather than a deletion.
- There is **no host-authored personal correction.** The host cannot amend the
  subject's own words.
- No automatic identity or standing-state promotion follows from it.
- **No personality governor.** It does not steer the agent toward particular
  conclusions, and it cannot manufacture compliance.
- It grants **no new external authority** — a directive cannot authorize an outward
  action that ordinary authority rules would refuse.
- **Repetition and elapsed time do not mechanically strengthen it.** Restating a
  preference does not raise its precedence, and time passing does not promote it.

---

## 12. Whole-package integration, and the Completion lesson

This is the most transferable section in the document.

### 12.1 What is not completion

A subject-facing capability is **not** complete because:

- backend code exists;
- a helper function exists;
- a unit test passes;
- a harness can call it manually;
- an isolated subsystem behaves correctly when exercised on its own.

### 12.2 What completion requires

Completion requires the **ordinary production path** to actually connect:

- the capability;
- provenance;
- authority;
- privacy and scope;
- persistence;
- restart and recovery;
- truthful failure semantics;
- and meaningful subject-facing affordance.

### 12.3 What actually happened

The final whole-package review found real integration defects in which individual
mechanisms existed, passed their own tests, and were nonetheless **not joined to
ordinary waking use**. Repairs were applied and the assembled package was then
accepted.

The project rule that came out of it:

> **Don't stop before it works.**
> **Don't keep going after you know it works.**

And the corollary that matters most for a reader:

> **Tests are evidence, not completion.**

A green suite is evidence about the things the suite exercises. It is silent about
the joins. Private review transcripts are not published; the architectural lesson is
what is being published here.

---

## 13. Substrate journey, and Ministral adoption

Factual and non-competitive. This is not a leaderboard and no model-comparison claim
is implied.

| Candidate | Mechanically | Outcome |
| --- | --- | --- |
| Formed OLMo | formation succeeded mechanically | **not adopted** — it failed the relational-history binding qualification, reading delivered history as a lookup result rather than binding it as its own past |
| Route A adaptation | stable adaptation mechanics established | **not adopted** — the intended comparative behavioral improvement was not established |
| Llama 3.1 8B Instruct Q4_K_M | served production through an intermediate architecture | part of the intermediate history, **not** the current waking substrate |
| Stock Ministral 3 14B Instruct 2512 Q4_K_M | qualified through the assembled production system | **adopted** as the waking substrate |

Two things worth separating:

**The OLMo result is the informative one.** Formation worked. Mechanical capability was
there. What failed was binding retrieved history to itself as its own past — which is
precisely the capability section 4 is about. A candidate can be mechanically
competent at interface formation and still fail at the relational thing, and a
qualification that only measures the former will pass it.

**Continuity does not reside in Ministral's weights.** It resides in the host-side
canonical record. Swapping substrates changes future inference and rewrites nothing.
Earlier events keep their own tag and digest.

Earlier substrates remain pinned and selectable as **rollback targets**. Rollback is a
distinct thing from history: it affects future inference only, and changes no durable
state.

---

## 14. RISE: what it taught, and what it did not establish

RISE — Reality-Grounded Initiative, Self-Direction & Epistemics — was an internal
evaluation and adaptation program. It was **not** part of the September public release
and is not a production subsystem.

Its continuing role is:

- **diagnostic material**;
- **semantic regression material**;
- **hard production-shaped cases**;
- and a historical record of what substrate and adaptation work taught.

### 14.1 The lesson that survived

**Not every desired behavior belongs in weights.**

Several behaviors that initially looked like things to train directly into model
weights turned out to depend on the complete agent trajectory: what evidence was
available, whether it was actually witnessed, whether the model could reconsider after
a canonical result, and whether the host had accidentally made a mechanical problem
look semantic.

That is a real limit on adaptation as a strategy. When a failure is at the
agent-system boundary, training against it optimizes the wrong thing.

### 14.2 Witnessing failure versus understanding failure

Preserved from 2 October, because it is the distinction that makes RISE interpretable
at all:

- **Witnessing failure** — the agent never encountered the relevant authoritative
  evidence. The remedy is in the interface.
- **Understanding failure** — the agent encountered it and interpreted it wrongly. The
  remedy may be semantic.

Collapsing these produces either training around interface defects, or dismissing a
genuine limitation as "the harness failed."

### 14.3 What RISE is not

RISE is **not** a mandatory training curriculum for every future substrate, and it is
not a maturity benchmark. Its cells are not evidence about the subject. Presenting
evaluation results as though they established something about personhood would
contradict the evidence/testimony boundary in section 15.

---

## 15. Evidence versus testimony

This boundary is maintained throughout the project and throughout this document.

The host may **mechanically establish**:

- occurrence;
- actor;
- source;
- provenance;
- timestamp;
- which model artifact served a turn;
- delivery;
- authority;
- scope;
- action receipts;
- the exact contents of a consolidation record.

Those facts do **not** mechanically establish:

- meaning;
- feeling;
- consciousness;
- personhood;
- identity;
- significance;
- relationship quality;
- subjective experience.

Two consequences are worth stating as principles, because they recur:

> **Acoustic measurement is not hearing.**

Waveform analysis of audio is decoding. It is not perception, and the project does not
claim otherwise.

> **Image delivery is not evidence of visual experience.**

Pixels were delivered is a delivery fact. What an image is like is not a delivery fact.

And one more:

> **Model self-description is not telemetry.**

A sentence a model generates about its own nature is not a measurement of anything,
including when it appears in that model's own turns.

---

## 16. What remains NOT_ESTABLISHED

Stated plainly, because an architecture document that only lists strengths is not an
architecture document.

- **Conscious experience, personhood, identity, feeling.** Not established. The host
  does not maintain a judgment about these, and these are not among the categories it
  records.
- **What any image or measurement is like.** Not established.
- **That the music pathway provides hearing.** It measures waveforms. That is all it
  establishes.
- **That long-term use will produce any particular personality, preference, or
  developmental outcome.** Not established, and longitudinal questions remain
  longitudinal. They cannot honestly be replaced by synthetic biography or benchmark
  theater.
- **The explicitly directed Sleep consolidation variant end-to-end.** Supported by the
  machinery; not exposed or exercised through the ordinary owner-authorized Sleep
  route.
- **Recovery lawfulness for a given failure.** May be *not established* rather than
  lawful or unlawful. The system records that honestly instead of guessing.
- **That substrate choice, adaptation, or evaluation predicts relational
  development.** The OLMo and Route A results are evidence about those candidates, not
  about what makes such development possible.

---

## 17. Portable lessons

What is meant to travel beyond this project's mechanisms:

1. **A retrieved past must be separated from the present, mechanically.** Otherwise old
   syntax silently becomes current authority.
2. **Authorship survives reconstruction.** If history is flattened into a transcript,
   who said what is lost, and the loss is silent.
3. **A subject's interpretation of its own history is testimony.** Build so that it
   structurally *cannot* be promoted into host fact.
4. **Additive retrieval cues should augment, never replace.** Preserve the ordinary
   results exactly, in order, with their original basis.
5. **"Stop preferring this" is not "erase this."** Separating de-emphasis from deletion
   is what lets an agent change its mind honestly.
6. **Significance is not disclosure authority.** Do not let a preference mechanic
   become a privacy leak.
7. **"Important to me" needs no host-side importance score.** The absence of such a
   field is a design commitment, not an omission.
8. **Uncertainty is a value worth storing.** "Not established" is often the truthful
   answer, and recording it beats guessing.
9. **Privacy is not proved by inspecting it.** Reduced observability is a feature.
10. **Tests are evidence, not completion.** Completion means the ordinary production
    path connects the capability to provenance, authority, privacy, persistence,
    recovery, failure truth, and real affordance.
11. **Not every desired behavior belongs in weights.** Diagnose the boundary before
    optimizing the model.
12. **Distinguish never-witnessed from mis-witnessed.** They have opposite remedies.
13. **Null is a real answer.** A system with no valid "nothing" produces activity that
    is not wanted.

---

## 18. Relationship to the September and 2 October records

Three dated layers, each truthful for its date:

| Document | Date | Standing |
| --- | --- | --- |
| `ANAXI_PROTOCOL_README_2026-09-24.md` | 24 Sep 2026 | historical production/release snapshot. Complete against its then-defined 36/36 release contract. |
| `ANAXI_CAPABILITY_CATALOG_2026-09-24.md` | 24 Sep 2026 | historical capability accounting for that release. |
| `reference-distribution/` | 24 Sep 2026 | frozen historical reproducibility package for that release. |
| `ANAXI_ARCHITECTURE_UPDATE_2026-10-02.md` | 2 Oct 2026 | the substrate-boundary correction. Its "adoption pending" language belongs to that date. |
| `claim-harness/FIELD_QUESTIONS_2026-10-02.md` | 2 Oct 2026 | portable questions from the substrate work. |
| **`ANAXI_ARCHITECTURE_UPDATE_2026-10-05.md`** | **5 Oct 2026** | **this document — the current public architecture.** |
| `claim-harness/FIELD_QUESTIONS_2026-10-05.md` | 5 Oct 2026 | portable questions from the whole-package work. |

None of the earlier documents were rewritten. Their current-state language was
accurate when written and is preserved as such. Where this document contradicts a
2 October statement about the present, this document is later and 2 October remains a
correct record of 2 October.

---

## 19. Public reproducibility boundary

**What this repository publishes:**

- architecture documentation at three dates;
- a creator's personal motivation essay (`WHY_ANAXI.md`), which is a personal
  document and not an empirical claim;
- the frozen September reference distribution;
- the standalone portable claim harness.

**What it does not publish:**

- the complete current private production runtime;
- private conversation transcripts, including the fact's content from the accepted
  waking interaction;
- private relational history;
- Private Space contents or metadata;
- family identities or content;
- correspondence identities or message contents;
- credentials, API keys, or provider secrets;
- production database contents;
- runtime logs;
- raw live-acceptance transcripts;
- local private filesystem paths;
- private longitudinal history.

`reference-distribution/` is a **frozen historical reproducibility package for the
24 September release**. It is not a complete reproduction of every mechanism added to
private production ANAXI after that date. A reader who wants the current architecture
should read this document, and should not infer from the reference distribution that
it reflects the whole current system.

---

## 20. What this architecture is not

ANAXI is not presented as a universal or normative agent architecture. Every
mechanism here is one project's answer to one set of problems.

It does not establish consciousness or personhood. It does not establish mature
long-term behavior. It does not establish that lived history will produce any
particular personality or preference. It does not establish that a non-human occasion
implies continuous cognition. It does not claim that a language model should be
trained to reproduce every desired behavior on its first response. It does not claim
that any future substrate should use these mechanisms. And it does not claim that
passing a test suite establishes completion.

> **Use the questions. Replace the mechanisms.**