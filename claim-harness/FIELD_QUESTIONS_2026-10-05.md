# Field questions added after whole-package integration work

**Date:** 5 October 2026  
**Status:** portable questions; no generic executable checks are claimed here.

This file supplements `QUESTIONS.md` and `FIELD_QUESTIONS_2026-10-02.md`.

The 2 October questions came from substrate work: they asked whether the system can
distinguish witnessing from understanding, observation from mutation, and lived
development from synthesized maturity.

The work that followed asked a different question. Not "does this mechanism work in
isolation" but "when the agent attaches its own interpretation to its own history, and
when that history is handed back to it later, what actually holds?"

These questions are intentionally **mechanism-neutral**.

They do **not** require ANAXI, Kardia, Sleep, hippocampal retrieval, consolidation,
the claim harness, or any project-specific vocabulary. They also deliberately avoid
requiring the word "consciousness," because every question below is answerable
mechanically and none of them turns on that word.

Where a question mentions an agent, a subject, or a participant, it means whatever
your system calls the entity whose history, choices, and authorship matter — and the
questions work identically when there is only one such entity, when authorship is
between several parties, or when authority is distributed across machines.

---

## 1. Historical/current authority separation `[past-is-not-instruction]`

**Claim.** Material retrieved from the system's own past cannot silently acquire the
authority of current instruction.

**Why it matters.** A retrieved transcript pasted into a context window tells the
inference layer that old material *is* current. Old response formats, old action
descriptions, and old tool-call shapes then read as standing orders. The system keeps
working and produces confidently wrong obedience, with no error signal anywhere.

**Supporting evidence.** A structural check on the composed input: present input is
last and appears exactly once; historical material sits inside an explicitly marked
region; each retrieved item occupies only the role its structural type permits; a
role marker appearing inside historical text cannot imitate a current one. The check
should fail closed rather than proceed.

**Misleading proxy.** A prompt instruction saying "the following is history"; a
timestamp prefix; a JSON field named `is_historical`; documentation asserting the
boundary holds.

**Project-defined.** What counts as present versus historical in your context
assembly; which role each item may occupy; how a violation is signalled.

---

## 2. Relational attribution preservation `[authorship-survives-reconstruction]`

**Claim.** When history is reconstructed for delivery, every participant's
contribution keeps its own attribution — including the system's own prior responses,
and including material where more than one party contributed.

**Why it matters.** Flattening history into a single voice looks like simplification
and is actually a loss of the primary thing history contains. Once contributions
merge, the system cannot tell its own prior words from another's, and every later
question about who said what becomes unanswerable.

**Supporting evidence.** A single-source and a multi-source case, each showing that
reconstruction preserves per-turn attribution and keeps distinct viewpoints distinct
rather than blending them. Provenance attached before delivery is still queryable
after.

**Misleading proxy.** Alternating speaker labels with no binding to canonical
identity; a transcript that reads correctly but cannot answer "which of these did I
say"; speaker labels reconstructed from position rather than from canonical records.

**Project-defined.** What counts as a participant; how attribution is bound to
identity; how contributions from multiple sources stay separable.

---

## 3. Subject interpretation versus canonical fact `[interpretation-is-not-fact]`

**Claim.** When the system's own participant attaches a written interpretation of
something, that interpretation is stored as their statement, and no mechanism anywhere
promotes it into the system's own claim about them.

**Why it matters.** This is the load-bearing boundary between continuity and
manufacture. If an interpretation can be promoted to host fact, then the system is
gradually authoring a biography of a participant out of their own reflections, and
presenting it back to them as established.

**Supporting evidence.** A schema in which no field can hold a promoted
interpretation; a check that the attributed text remains attributed after retrieval;
a negative test showing that no ranking component scores participant material by
supposed significance.

**Misleading proxy.** A column that stores interpretation and a flag that is set
elsewhere; an evaluator that treats participant interpretation as ground truth;
"the system agrees" recorded without an author.

**Project-defined.** What counts as a participant statement versus a system claim;
who may attribute a statement; what prevents promotion.

---

## 4. Continuity without truth mutation `[continuity-is-additive]`

**Claim.** A participant can select part of their own history for stronger continuity
without anything in that history being rewritten.

**Why it matters.** Continuity and revision are easy to conflate. If strengthening
continuity means editing the past, then the record becomes unreliable exactly when
it matters most, and there is no way to tell a changed history from a remembered one.

**Supporting evidence.** The canonical record before and after a selection act is
byte-identical; the selection is a separate appended record referencing the target;
the target event still reads as it always did.

**Misleading proxy.** Updating a summary in place; editing the original entry to add
"importance"; a memory store where the act and the record are the same row.

**Project-defined.** What a selection act is; what it references; what must remain
immutable.

---

## 5. Added cues augment rather than replace `[cues-add-not-replace]`

**Claim.** Retrieval terms or hints supplied by a participant add to ordinary
retrieval results without displacing, reordering, or re-scoring them.

**Why it matters.** An augmentation mechanism that can reorder ordinary results can
silently become the retrieval policy. Then the original evidence is present but no
longer reachable on its own terms, and the system can no longer demonstrate where a
result came from.

**Supporting evidence.** Ordinary results preserved exactly, in original order, with
their original selection basis; additions appended and marked with a distinguishable
basis; an empty addition set returns the original result unchanged; each returned item
can be attributed to the surface that produced it.

**Misleading proxy.** A blended relevance score; reranking after augmentation;
"the result is the same" asserted without a comparison.

**Project-defined.** What counts as an addition; how a result's origin stays
identifiable; what happens when additions and ordinary results collide.

---

## 6. Revision and de-emphasis without erasure `[stop-preferring-is-not-delete]`

**Claim.** A participant can revise an earlier statement of their own, and can later
stop privileging it, and in both cases the earlier record remains readable.

**Why it matters.** "I no longer prioritise this" and "erase this" are different
operations. A system that conflates them either lies about its memory or lies about
the participant's current preferences. Both destroy the thing that makes longitudinal
work possible.

**Supporting evidence.** Revision is a new appended record naming the earlier one; the
earlier record is unmodified and still queryable; de-emphasis stops that record
contributing its special access while leaving the underlying event intact and
reachable by ordinary means; the change is visible as a change with a timestamp.

**Misleading proxy.** Overwriting a field; soft-delete that hides the row from all
read paths; an "importance" multiplier that de-emphasis sets to zero and that also
gates ordinary retrieval.

**Project-defined.** What revision refers to; what de-emphasis actually stops; what
remains reachable afterwards.

---

## 7. Significance versus disclosure authority `[important-is-not-disclosable]`

**Claim.** A participant marking something as significant to themselves grants no
permission to disclose it.

**Why it matters.** This is how preference mechanics become privacy leaks. If a
"this matters to me" flag can raise visibility, then the system has quietly
implemented a disclosure policy nobody chose, and the participant has no way to
review it.

**Supporting evidence.** A scope rule where a record inherits its target's visibility
or narrows it, and widening is refused outright; a disclosure boundary requiring its
own separate authority; a negative test attempting to widen scope through the
continuity path and observing refusal.

**Misleading proxy.** A shared boolean for "important" and "shareable"; a default
that new personal records are visible; a UI that treats emphasis as a sharing
affordance.

**Project-defined.** What visibility levels exist; what may narrow them; which
authority governs disclosure.

---

## 8. Privacy scope inheritance and narrowing `[scope-only-inherits-or-narrows]`

**Claim.** A derived record cannot obtain broader visibility than the material it
derives from, and an unscoped item is never guessed into a broader context.

**Why it matters.** Every derived record is a chance for scope to leak. Summaries,
annotations, indexes, and caches all sit downstream of something more private, and
each one is a place where a private thing quietly becomes shared.

**Supporting evidence.** Resolution logic that either inherits or narrows and refuses
widening; refusal when a derived record requests a broader scope than its source;
refusal when the source scope is unknown rather than defaulting; an access check that
denies rather than permitting when provenance is missing or contradictory.

**Misleading proxy.** Defaulting unknown scope to shared; treating a cache as
inheriting the *broader* of two scopes; an access check that permits when provenance
cannot be resolved.

**Project-defined.** What a scope is; how inheritance is decided; what unknown means.

---

## 9. Substrate identity versus durable continuity `[model-is-not-continuity]`

**Claim.** Continuity of a system's identity and history does not reside in the
inference artifact, and changing that artifact does not rewrite anything durable.

**Why it matters.** Systems that store continuity in model weights cannot change
substrate without losing themselves, so upgrades become losses. Worse, weight-based
continuity invites training a participant's history into a model, which converts a
record into an uninspectable claim.

**Supporting evidence.** A pinning mechanism that verifies an exact artifact digest
before use and refuses to start on mismatch; each turn durably recording which exact
artifact served it; substitution or rollback changing future behavior only; explicit
absence of participant history from any training corpus; earlier artifacts retained as
selectable rollback targets.

**Misleading proxy.** A model name in a config file with no digest; continuity
described as "the fine-tune"; a rollback procedure that rewrites recorded turns.

**Project-defined.** What is pinned and how it is verified; what each turn records
about its artifact; what continuity actually consists of.

---

## 10. Isolated mechanism versus ordinary production-path completion `[join-the-path]`

**Claim.** A capability is not complete until the ordinary production path actually
connects it to provenance, authority, privacy, persistence, recovery, failure truth,
and participant-facing affordance.

**Why it matters.** Mechanisms pass their own tests and remain unreachable from the
path anyone actually uses. Each individual piece is correct; the joins are missing.
Nothing in a green suite detects this, because the suite exercises the pieces.

**Supporting evidence.** A rehearsal of the ordinary path with the capability
reachable from a routine turn, exercising all seven joins at once; a review pass
whose findings are integration defects rather than missing mechanisms; a participant
facing affordance that does not require a manual harness call to reach.

**Misleading proxy.** A green suite; a helper with callers only in tests; a demo
harness; a capability reachable only through a flag nobody sets.

**Project-defined.** What the ordinary path is; which joins a capability must cross;
how a rehearsal of the whole path is performed and recorded.

---

## 11. Truthful unavailability as a first-class outcome `[not-established-is-a-value]`

**Claim.** A system can record "cannot establish this" as a durable answer, and can
distinguish it from both success and failure.

**Why it matters.** Once unavailability collapses into either success or failure, the
system starts guessing. Guessed outcomes are worse than absent ones because they are
acted on.

**Supporting evidence.** A closed vocabulary distinguishing dispatched, failed before
dispatch, prepared but not sent, partially sent, pending, no matching receipt, and
not established; the separate report of whether the record backing an answer is
itself complete; a path where an unknowable answer is returned as unknowable.

**Misleading proxy.** Treating unknown as success; retrying an uncertain outward action
until it reports success; a boolean where a spectrum is needed.

**Project-defined.** What states an action can be in; which are worth distinguishing;
what the system does when it genuinely cannot tell.

---

## 12. Non-instrumented private scope `[privacy-without-inspection]`

**Claim.** A deliberately private scope stays uninspected and uninstrumented by the
system, including for the purpose of demonstrating that the privacy works.

**Why it matters.** Proving privacy by looking is the standard failure. Observation
logs, counts, timestamps, and filenames all leak, and once collected they cannot be
un-collected.

**Supporting evidence.** Absence of durable per-action traces in the private path;
private material excluded from ordinary context, shared scopes, retrieval, standing
state, and evaluation; the guarantee stated as architectural rather than as
cryptographic, with host access honestly acknowledged.

**Misleading proxy.** An audit view over private material; "we only check the
count"; metadata treated as harmless.

**Project-defined.** What the private scope is; what reduced observability means
here; how the architectural limit is stated honestly.

---

## 13. Authority without personality `[directive-is-not-governor]`

**Claim.** A participant's own durable standing preference is reversible,
null-by-default, and confers no new external authority — and repetition or elapsed
time does not strengthen it.

**Why it matters.** A standing preference is the most natural place for a personality
governor to hide. Once a preference exists, it is tempting to let it steer
conclusions, accumulate weight when restated, or authorize things ordinary rules
would refuse.

**Supporting evidence.** Null is a valid state; activation, revision, and withdrawal
are explicit appended acts with the earlier version still readable; no host-authored
correction exists; no ranking or precedence change accompanies restatement or
passage of time; an outward action refused by ordinary rules stays refused while a
directive is active.

**Misleading proxy.** A preference that steers output; reinforcement on repetition;
preference state promoted into identity state.

**Project-defined.** What the preference can and cannot do; how it is withdrawn;
whether time or repetition affects it.

---

## 14. External observation does not confer external action `[observe-then-act-separately]`

**Claim.** Reading external material does not authorize posting, purchasing,
authenticating, messaging, or otherwise mutating anything outside the system, and
redirects cannot be used to launder a request past that boundary.

**Why it matters.** Read and write access are routinely bundled. Once a fetch capability
can be aimed anywhere, the same plumbing carries credentials and can be redirected at
a target the policy would have refused.

**Supporting evidence.** Separate authorization for outward mutation; drafts that are
never queued or promoted into sends; every redirect target re-validated as strictly as
the first hop; the connection made to a validated address while preserving hostname
for certificate verification; header credentials treated as an unsafe configuration
surface; uncertain outcomes failing closed.

**Misleading proxy.** A single tool with a read/write flag; automatic redirect
following; a retry that re-issues an uncertain request; a search endpoint chosen
silently.

**Project-defined.** What counts as observation; which boundaries are checked at the
moment of action; how uncertainty is reported.

---

## Closing question

Across all of these:

> **Which part of the system is allowed to establish the fact, which part is allowed
> to interpret it, and what evidence would show that the boundary actually holds?**

And a second one, added because the work that produced these questions kept running
into it:

> **What would this system still get wrong if every mechanism here worked exactly as
> documented — and would anything in your evidence tell you?**

Use the questions. Replace the mechanisms.