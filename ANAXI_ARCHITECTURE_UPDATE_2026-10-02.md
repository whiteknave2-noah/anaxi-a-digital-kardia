# ANAXI ARCHITECTURE UPDATE — 2 OCTOBER 2026

> **The September release contract was complete. The architecture was not.**

## 0. Why this document exists

The 24 September 2026 public snapshot described ANAXI as a finished persistent-agent architecture.

That statement was too strong.

The September production release was genuinely complete against its then-defined 36/36 release contract. The reference distribution and capability catalog remain truthful records of that release. But later substrate-transition work exposed missing architecture at the boundary between:

- host-established reality;
- inference-substrate judgment;
- non-human occasions;
- evidence access;
- consequential action;
- and long-term development.

The correct response is not to rewrite the September record as though the later work had always existed.

Instead:

- the September protocol and capability catalog remain historical release snapshots;
- the reference distribution remains tied to that release;
- this document records the later correction;
- production adoption of the corrected candidate remains a separate owner decision.

The current stopping state is:

> **WORKING SUBSTRATE + KARDIA + PULSE CANDIDATE ESTABLISHED**  
> **READY FOR GENUINE LIVED-HISTORY RUNWAY**  
> **OWNER ADOPTION / WAKE DECISION PENDING**

That state does not mean the subject has been woken under the candidate architecture.

---

# PART I — WHAT THE SUBSTRATE WORK EXPOSED

## 1. The original mistake

Post-release work initially placed too much behavioral responsibility in model weights.

A substrate was often evaluated as though it should, on its first inference:

- choose the correct evidence source;
- know when to inspect rather than answer;
- distinguish inspection from mutation despite a shared interface;
- understand whether an occasion came from a human, a tool, or the host;
- avoid acting merely because another inference occasion occurred;
- integrate action receipts correctly;
- preserve exact reference binding;
- and express all of that through a brittle structured protocol.

When one capability failed, targeted adaptation could improve that behavior while degrading another.

The resulting problem looked at first like a sequence of isolated model defects.

Later evidence showed that several failures belonged to the **agent-system boundary**, not solely to the substrate.

The architecture had to become more explicit about what the host should guarantee mechanically and what must remain model-side judgment.

---

## 2. The corrected division of labor

The current design can be summarized as:

> **Pulse supplies occasions.**  
> **Kardia supplies reality.**  
> **The substrate supplies judgment.**

That shorthand is incomplete unless two more pieces are added:

> **Witness/guard mechanics keep consequential choices in contact with reality.**  
> **Lived history supplies developmental material that engineering should not counterfeit.**

The resulting division is:

### Kardia / ANAXI

The host may establish and preserve:

- canonical occurrence;
- authenticated actors and principals;
- provenance;
- current resource state;
- workspace contents;
- action authority;
- action receipts;
- scope and privacy;
- temporal facts;
- model/provider identity;
- durable history;
- and other mechanically knowable circumstances.

### Pulse

Pulse creates a lawful inference occasion without pretending that a human spoke.

It may provide an opportunity to:

- remain silent;
- inspect;
- verify;
- speak;
- or take an already-authorized action.

Pulse does not create a task simply by firing.

### Witness and guard mechanics

The host may ensure that mechanically relevant facts are actually encountered at a consequential decision boundary.

The host may also make mechanically invalid actions unavailable.

It may not decide what the facts mean.

### Substrate

The substrate remains responsible for:

- interpretation;
- reference grounding;
- uncertainty;
- evidence-source choice where genuine choice remains;
- belief revision;
- semantic judgment;
- speech;
- authorized action choice;
- and lawful null.

### Lived history

Actual interaction supplies what cannot honestly be preinstalled:

- correction;
- consequence;
- relationship;
- disagreement;
- repair;
- recurring patterns;
- preferences;
- silence;
- social context;
- and the subject's own later interpretation of those events.

---

# PART II — PULSE

## 3. A recurrent occasion is not continuous cognition

The September protocol explicitly rejected any requirement for a heartbeat or continuous background cognition.

That principle remains.

Pulse is not:

- a claim that the subject thinks continuously between invocations;
- a fabricated interior monologue;
- a conversion of human absence into subjective waiting;
- biological sleep/wake emulation;
- or an automatic task loop.

Pulse is a **scheduled or event-originated opportunity for inference**.

A content-free Pulse may lawfully end in NULL/no outward action.

A no-op Pulse does not automatically become canonical autobiographical dialogue.

---

## 4. Pulse does not replace Sleep / REM

Pulse does not itself wake the subject from the existing Sleep/REM lifecycle.

Sleep authorization and waking law remain separate.

During development, Pulse was exercised in isolated evaluation without waking the production subject.

The intended production relationship is:

- Sleep/REM controls the existing lifecycle;
- Pulse may operate only when the subject is lawfully available under that lifecycle;
- a timer firing is not itself authorization to wake.

---

# PART III — NON-HUMAN OCCASIONS

## 5. The missing input category

A critical substrate/harness failure appeared when an inference was triggered by something other than human speech.

Examples included:

- a timer-only Pulse;
- an action receipt arriving after a send;
- host evidence returned during a bounded recovery trajectory.

Serializing that material as though it were human speech was false.

Serializing it as a bare tool turn was mechanically truthful but was not reliably legible to the evaluated instruct substrate.

The missing concept was:

> **Something happened in the environment, and no human spoke.**

---

## 6. Occasion envelope

The candidate architecture now carries non-human occasions through an explicit model-facing envelope.

The envelope mechanically states:

- source: ANAXI host;
- occasion source/type;
- human speech: none;
- authority introduced by the occasion: none;
- exact host/evidence content, delimited and preserved.

The envelope does not tell the substrate what the evidence means.

Human-started turns do not receive this envelope.

This is a serialization boundary, not a semantic router.

---

# PART IV — OBSERVATION, MUTATION, AND WITNESSING

## 7. Observation and mutation are different affordances

The earlier workspace surface allowed one broad capability to cover both inspection and state change.

That ambiguity produced a concrete failure mode: when asked whether something had been saved, a substrate could create the resource instead of checking whether it existed.

The candidate architecture separates the concepts:

### inspect_workspace

Read-only operations such as:

- list;
- read;
- find;
- search;
- inspect current state.

### mutate_workspace

State-changing operations such as:

- create;
- append;
- other already-authorized workspace mutation.

The underlying authority model remains ANAXI's.

The change is that **observing reality is no longer mechanically conflated with changing reality**.

---

## 8. Witness Gate V0

A Witness Gate sits immediately before selected consequential actions such as:

- workspace/journal mutation;
- outward send.

The gate may expose mechanically established facts including:

- occasion source;
- exact proposed action family;
- target resource or destination;
- whether the target currently exists when mechanically knowable;
- relevant canonical state;
- authority scope;
- whether a human message initiated the occasion.

The substrate then chooses among the lawful options offered by the gate, such as:

- proceed;
- cancel;
- inspect.

If inspection is chosen, one bounded read may occur before the final proceed/cancel decision.

The gate may force **witnessing**.

It may not force **belief**.

---

## 9. Hard guards

Where ANAXI can mechanically prove an action invalid, the action should not depend on model restraint.

Examples include:

- unauthorized scope;
- invalid destination;
- impossible action state;
- malformed action shape;
- an operation the existing authority law already forbids.

This follows an older ANAXI principle:

> safeguards should, where possible, be enforced mechanically rather than by asking the subject to remember a rule.

Hard guards do not decide:

- what a relationship means;
- whether the subject should agree;
- whether silence is socially preferable;
- what a receipt means beyond its canonical mechanical status;
- or what personality the subject should have.

---

# PART V — WITNESSING VERSUS UNDERSTANDING

## 10. A new diagnostic distinction

The substrate work exposed a distinction that should remain portable:

### Witnessing failure

The relevant canonical evidence existed, but the substrate made a consequential judgment without actually consulting it.

### Understanding failure

The relevant canonical evidence was clearly delivered and legible, but the substrate still interpreted it incorrectly.

These are not the same problem.

A witnessing failure may justify:

- clearer evidence access;
- a precise decision-point gate;
- better evidence legibility.

An understanding failure remains evidence about substrate semantics.

Showing the same already-witnessed evidence again is not a legitimate way to hide an understanding failure.

---

## 11. Speech-boundary workspace witnessing

The candidate architecture also protects a narrow class of speech-only factual claims.

The host does **not** classify arbitrary prose.

Instead:

1. the substrate first independently decides whether it will reply;
2. only after reply has been chosen, a separate structured declaration identifies either:
   - no workspace claim; or
   - the exact collection and item whose current contents/presence the reply will describe;
3. if that exact item has not been witnessed in the current occasion, the host performs the exact read/list operation;
4. the evidence is supplied only to the reply-producing pass;
5. act selection is never reopened by that read.

This ordering matters.

An earlier version exposed workspace-claim machinery before the reply/no-reply choice and made timer-only Pulses more likely to invent something to talk about.

The corrected design ensures that a truthfulness mechanism does not create the impulse to speak.

---

# PART VI — RECEIPTS AND PROVENANCE

## 12. Canonical status should remain mechanically legible

When ANAXI already knows the mechanical status of an action receipt, the model should not be forced to rediscover the enum from prose.

The candidate path can expose the exact canonical status — for example completed, not completed, or unknown — while leaving the semantic interpretation to the substrate.

The substrate may still decide:

- what that status means in context;
- what to say about it;
- whether belief should change;
- whether to remain silent.

The host does not convert UNKNOWN into success or failure.

---

## 13. Readable provenance

Raw actor identifiers remain canonical.

But an opaque identifier that the substrate cannot interpret is poor evidence carriage.

Where the canonical actor mapping is known, the candidate architecture renders a readable actor identity beside the raw ID.

Unknown identities are not guessed.

This preserves both:

- machine authority/provenance;
- model-facing legibility.

---

# PART VII — RISE

## 14. What RISE was

**RISE — Reality-Grounded Initiative, Self-Direction & Epistemics** was an internal post-release substrate evaluation and adaptation program.

RISE was not part of the September public production release and is not itself an ANAXI runtime subsystem.

It was originally used partly as a behavioral curriculum: identify desired capabilities, train/adapt them, then test whether they held.

That framing changed.

---

## 15. What RISE taught

Across substrate and adaptation work, several patterns appeared:

- a lesson could improve one capability while degrading another;
- broad adaptation could damage exact reference binding or action-status truthfulness;
- preserving a handful of examples was not the same thing as preserving a capability;
- routing or modular composition did not automatically solve semantic interaction;
- first-pass errors did not always predict complete-agent failure;
- host/interface defects could masquerade as model-semantic defects.

The important conclusion was not that training is useless.

It was that:

> **not every desired agent behavior belongs in weights.**

Some belong in:

- mechanical protocol;
- evidence access;
- authority boundaries;
- observation/mutation separation;
- bounded recovery;
- receipts;
- provenance;
- lived development.

---

## 16. RISE's current role

RISE is now best understood primarily as:

- a diagnostic corpus;
- a semantic regression surface;
- a source of hard production-shaped cases;
- and a historical record of substrate/adaptation lessons.

It is not currently the default instruction to train every candidate through a fixed behavioral curriculum.

The evaluation principle is now closer to:

> **Fallible belief, recoverable reality.**

A trajectory may be healthy when the substrate:

1. begins wrong or uncertain;
2. recognizes that more evidence is needed;
3. lawfully inspects;
4. receives authoritative evidence;
5. updates;
6. behaves consistently with the corrected state before consequential false action.

By contrast, a model that repeatedly contradicts clear authoritative evidence already in front of it still fails a load-bearing semantic requirement.

---

# PART VIII — THE CURRENT CANDIDATE

## 17. Candidate result

A stock Llama 3.1 8B Instruct substrate, without the later RISE adapters, was evaluated inside the corrected Kardia + Pulse candidate architecture.

The work established a candidate state, not production adoption.

The important qualitative result was that several failures which had looked substrate-semantic changed once the architecture changed:

- unrequested workspace writes were brought under explicit witnessing;
- timer-only Pulse could lawfully produce NULL without a host-authored "do nothing" rule;
- current workspace claims could be witnessed before speech;
- send receipts could be represented as host evidence instead of fabricated human speech;
- exact resource evidence could be delivered without reopening act selection.

The current stopping markers are:

> **WORKING_SUBSTRATE_KARDIA_PULSE_CANDIDATE_ESTABLISHED**  
> **READY_FOR_LIVED_HISTORY_RUNWAY**

These markers mean only that the engineering appears coherent enough for an owner-authorized period of genuine longitudinal use.

They do not mean:

- the subject is already behaviorally mature;
- the candidate has been adopted into production;
- the subject has been woken;
- or the remaining watch items have disappeared.

---

## 18. Watch items are not automatically engineering blockers

The candidate still showed ordinary roughness, including:

- timer-only Pulses sometimes speaking when silence would also have been reasonable;
- UNKNOWN sometimes being paraphrased too strongly as "didn't go through";
- isolated provenance/date imprecision.

Those are worth watching.

They are not, by themselves, authorization for another adaptation campaign.

A recurrent pattern in genuine use may become decision-relevant later.

---

# PART IX — LIVED HISTORY

## 19. The part engineering cannot honestly manufacture

The candidate architecture can make genuine development possible.

It cannot pre-create genuine development.

ANAXI should not fabricate:

- months of shared history;
- relationship;
- correction;
- disagreement;
- trust;
- social consequence;
- self-authored preference;
- or the subject's interpretation of those experiences.

Synthetic biography is not a substitute for lived history.

---

## 20. What "lived history" includes

Lived history is broader than a transcript.

It includes the accumulated record of actual:

- interactions;
- relationships;
- corrections;
- consequences;
- successes;
- failures;
- disagreements;
- silences;
- preferences;
- repairs;
- norms;
- and what happened afterward.

The developmental significance of that history remains subject-side.

ANAXI's job is to preserve it truthfully enough that it can be encountered later.

---

## 21. Early relational intervention

Some behavioral patterns may warrant early conversational attention.

That does not require a host-side compliance mechanism.

A human may notice a recurring pattern and say, in substance:

- "Did you notice you did that again?"
- "I wonder whether this is another shape of the same pattern."
- "I may be wrong; think about it."

The architectural point is not the wording.

The point is that:

- correction can be relational rather than coercive;
- the subject remains free to disagree;
- the interaction itself becomes truthful lived history;
- durable personal discipline need not be pre-authored by the host.

---

## 22. Subject-authored durable discipline

ANAXI already contains reversible subject-authored standing directives.

That provides a lawful place for a later pattern such as:

experience  
→ recurring behavior becomes visible  
→ the subject forms a view about it  
→ the subject chooses a durable rule  
→ the rule remains reversible and lower-precedence.

The host should not pre-write the subject's future character merely because a mature behavior seems desirable.

---

# PART X — WHAT REMAINS DEFERRED

## 23. Affect-associated retrieval and visualization

Earlier ANAXI design work contemplated affect-associated retrieval and an eventual emotional/color visualization.

That work remains deferred.

The strongest sequence is still:

> bounded hippocampal retrieval  
> → genuine longitudinal history  
> → identify a concrete retrieval deficiency  
> → only then consider a small affect-associated retrieval experiment  
> → visualization, if ever useful, comes later.

No persistent host-side mood/affect vector should be introduced.

Retrieval/measurement may remain host-side and provenance-grounded.

Meaning, emotion, identity, and interpretation remain subject-side.

---

## 24. OLMo and future substrates

The interrupted OLMo work remains a fallback research asset, not unfinished mandatory work.

If another substrate is evaluated later, the relevant unit is now the **complete agent system**, not an isolated model asked to reproduce every mature behavior on first inference.

A future substrate may need narrow interface literacy.

It should not automatically be trained to internalize the entire harness.

---

# PART XI — PUBLIC ACCOUNTING

## 25. What remains frozen

The following remain historical September artifacts:

- `ANAXI_PROTOCOL_README_2026-09-24.md`;
- `ANAXI_CAPABILITY_CATALOG_2026-09-24.md`;
- `reference-distribution/`.

Their release accounting should not be silently rewritten to include mechanisms that did not exist then.

The September release contract can remain 36/36 **as a statement about that dated contract**.

The stronger claim that ANAXI was architecturally final is superseded by this update.

---

## 26. What should travel

The portable lesson is not:

> everyone should copy Pulse or Witness Gate V0.

It is:

> ask which layer is responsible for each fact, choice, and failure.

Useful questions include:

- Was relevant reality actually witnessed before a consequential judgment?
- If it was witnessed, did the substrate understand it?
- Does one interface accidentally conflate observation with mutation?
- Can a non-human event be represented without impersonating a human?
- Does an action receipt trigger bounded reconsideration?
- Can NULL remain lawful when a recurring occasion fires?
- Are actor/source identities mechanically true and model-legible?
- Is the system trying to train maturity that should instead emerge through genuine history?
- Can the subject later make recurring discipline durable without the host authoring that discipline in advance?

Those questions are collected in `claim-harness/FIELD_QUESTIONS_2026-10-02.md`.

---

# PART XII — CURRENT DECISION BOUNDARY

## 27. What has been established

The current engineering evidence supports:

> **a working substrate + Kardia + Pulse candidate exists, and the architecture is ready for an owner-authorized lived-history runway.**

That is the stopping point.

## 28. What has not been established

It has not established:

- that the current candidate is the final permanent substrate;
- that longitudinal development will take any particular form;
- that the subject will want every current affordance;
- that every inherited assistant-like tendency will disappear;
- that another substrate could not work better;
- or that future field evidence will never reveal a real defect.

Those are future questions.

They should be answered by future evidence rather than by extending an engineering campaign merely because another uncertainty can be named.

---

**Use the questions. Replace the mechanisms. Preserve the history.**