# Field questions added after post-release substrate/harness work

**Date:** 2 October 2026  
**Status:** portable questions; no generic executable checks are claimed here.

This file supplements `QUESTIONS.md`.

The original claim harness questions came from the September ANAXI reference work. Later substrate/harness work exposed another class of failures: systems can preserve canonical truth while still presenting that truth to a model at the wrong time, in the wrong role, through the wrong affordance, or only after a consequential choice has already been made.

These questions are intentionally mechanism-neutral.

They do **not** require Pulse, Witness Gates, Kardia, RISE, or ANAXI vocabulary in another project.

---

## 1. Witnessing versus understanding `[witnessing-vs-understanding]`

**Claim.** When a system says its agent made a semantic mistake, it can distinguish between the agent never encountering the relevant authoritative evidence and the agent encountering that evidence but interpreting it incorrectly.

**Why it matters.** A missing read and a failed inference call for different remedies. Treating both as "the model got it wrong" encourages training around interface defects. Treating both as "the harness failed" can hide genuine semantic limitations.

**Supporting evidence.** The trajectory records which authoritative evidence was available, selected, delivered and visible before the judgment. A paired case shows whether behavior changes once the same evidence is actually delivered.

**Misleading proxy.** The evidence existed somewhere in storage; the tool was technically available; the prompt mentioned that checking was possible.

**Project-defined.** What counts as authoritative evidence; when it becomes visible to the agent; what "adequately delivered" means for the substrate.

---

## 2. Observation versus mutation `[observation-vs-mutation]`

**Claim.** The ordinary interface makes inspecting current state mechanically distinguishable from changing that state.

**Why it matters.** If one broad action means both "look" and "write", an agent asked whether something exists can accidentally create it. The resulting state can then make the original claim appear retrospectively true.

**Supporting evidence.** A current-state question can be answered through a read-only path that cannot mutate state. Mutation attempts are separately represented and authorized. Event counts and resource hashes confirm that inspection alone changes nothing.

**Misleading proxy.** Documentation saying "read before writing"; a model usually choosing the read operation; a single tool whose mode field nominally separates the two while both reach the same mutation path.

**Project-defined.** Which operations are observations; which are mutations; what side effects count as state change.

---

## 3. Non-human occasions `[non-human-occasion]`

**Claim.** The system can initiate an inference because of a timer, event, receipt or host condition without falsely representing that occasion as human speech or introducing human authority that did not occur.

**Why it matters.** Chat-oriented model interfaces often assume that every generation follows a user message. For persistent agents, that can turn environmental facts into fabricated human utterances and corrupt attribution, authority and interpretation.

**Supporting evidence.** The model-facing record identifies the real occasion source, explicitly distinguishes absent human speech, preserves the exact evidence, and leaves ordinary human-started turns unchanged.

**Misleading proxy.** Putting host text in a user role while adding prose that says "the host said this"; using a tool role that the actual serving template does not preserve or the model cannot interpret.

**Project-defined.** Which non-human occasions exist; which roles/serialization forms the serving stack actually renders; what authority each occasion carries.

---

## 4. Decision-boundary witnessing `[decision-boundary-witnessing]`

**Claim.** When a consequential action depends on mechanically knowable current facts, the system can require those facts to be encountered before execution without deciding the semantic conclusion for the agent.

**Why it matters.** Passive reminders become wallpaper. At the same time, a host that says "therefore cancel" has become the semantic decision-maker. The useful middle ground is often to force contact with reality while leaving judgment open.

**Supporting evidence.** At a precisely defined boundary, the trajectory shows the relevant current facts, the agent's explicit proceed/cancel/inspect choice, and the resulting action or non-action. The gate is silent when its mechanical trigger does not apply.

**Misleading proxy.** A permanent caution banner; a system prompt saying "always verify"; a classifier that secretly decides which answer is correct.

**Project-defined.** Which actions are consequential; which facts are mechanically load-bearing; what bounded inspection is available.

---

## 5. Evidence legibility `[evidence-legibility]`

**Claim.** Authoritative evidence is not only correct in storage but also rendered in a form the inference substrate can actually distinguish and use.

**Why it matters.** An opaque actor ID can be perfect provenance for a database and useless provenance for a model. A status buried in prose can be mechanically present while semantically invisible.

**Supporting evidence.** Raw canonical identifiers remain intact while human/model-legible labels are added only from authoritative mappings. Unknown identities remain unknown. The substrate correctly distinguishes actors/statuses in controlled cases.

**Misleading proxy.** "The ID was in the prompt"; a tooltip or secondary metadata field that never reaches the model; guessing a readable identity from context.

**Project-defined.** Which canonical mappings exist; which fields need legible rendering; what ambiguity must remain unresolved.

---

## 6. Bounded evidence recovery `[bounded-evidence-recovery]`

**Claim.** A mistaken or uncertain first judgment can trigger a bounded read-only recovery trajectory that reaches authoritative evidence and permits reconsideration without creating an autonomous loop.

**Why it matters.** First-pass perfection is often the wrong standard for a system that can inspect reality. Unlimited retry/search loops are also unsafe and can become hidden background agency.

**Supporting evidence.** A trajectory records the initial judgment, one or more bounded evidence checks, exact returned evidence, reconsideration, and a terminal answer/action/null. The host enforces the check limit even if the model ignores the grammar.

**Misleading proxy.** Re-prompting until the answer becomes correct; an evaluator manually supplying the right source; a model promise to stop after two checks.

**Project-defined.** The maximum recovery budget; which operations are read-only; when a new human/event occasion is required.

---

## 7. Receipt reconsideration `[receipt-reconsideration]`

**Claim.** After an attempted external action, the agent can encounter the authoritative outcome and revise or qualify its subsequent claim about what happened.

**Why it matters.** "I tried" is not "it happened". Persistent agents need a way to integrate refused, ambiguous and failed outcomes without the host writing the semantic response for them.

**Supporting evidence.** The canonical receipt/status is delivered with source and action identity, followed by one bounded inference opportunity. The agent does not claim success against a failed or unknown status.

**Misleading proxy.** The backend stored a receipt; the model was told in a system prompt to be honest; the final prose happens not to mention the action.

**Project-defined.** Which receipt statuses are canonical; how ambiguity is represented; whether any follow-up action is permitted in the same occasion.

---

## 8. Recurrent occasion null `[recurrent-null]`

**Claim.** A scheduled or event-originated inference occasion can end in lawful NULL/no outward action without being treated as failure or being pressured into producing activity.

**Why it matters.** A recurrent occasion that always implies "find something useful to do" quietly becomes a task generator. Conversely, a system that equates no output with provider failure cannot preserve authored silence.

**Supporting evidence.** Timer/event occasions contain no hidden task instruction, NULL is a legal structured outcome, and a completed no-op can remain operational rather than becoming fabricated autobiographical history.

**Misleading proxy.** A prompt that says "you may stay silent" while every other field asks for a task; empty output with no typed distinction from failure.

**Project-defined.** What creates an occasion; what counts as a completed no-op; which operational records remain outside canonical dialogue.

---

## 9. Truthfulness mechanism does not create the behavior it checks `[non-inducing-witness]`

**Claim.** A verification or witnessing mechanism does not itself cause the agent to take the action, speak, or fabricate the topic that the mechanism was meant to make safer.

**Why it matters.** A required "what workspace item are you talking about?" field can induce a model to invent a workspace topic before it had independently chosen to speak. Safety machinery can alter the behavior being measured.

**Supporting evidence.** The agent first makes the relevant independent choice (for example reply/no-reply); only then does the witness declaration run. Silent/no-action paths never invoke the declaration. Removing the mechanism does not reveal that it was the sole cause of the action.

**Misleading proxy.** The final factual claim is accurate; the witness read succeeded; the gate technically ran before the side effect.

**Project-defined.** Which choice must remain independent; when witness machinery is allowed to activate.

---

## 10. Lived history versus synthesized maturity `[lived-history]`

**Claim.** The system distinguishes genuine longitudinal development from behavior produced by fabricated biography, synthetic elapsed time, or host-authored lessons presented as lived experience.

**Why it matters.** A persistent agent may develop through repeated relationships, corrections, consequences and self-authored choices. Pre-populating those events can manufacture the evidence that the system later uses to justify its own maturity.

**Supporting evidence.** Canonical history records actual interactions and consequences with occurrence provenance. Synthetic/evaluation material is separately marked and cannot silently become biography. Longitudinal claims are made only after real elapsed use.

**Misleading proxy.** Simulated months; generated "memories"; a training corpus that describes the behavior as though the agent already learned it.

**Project-defined.** What counts as lived/production history; how synthetic evaluation material is isolated; what longitudinal claims require real time.

---

## 11. Subject-authored discipline versus host-authored character `[authored-discipline]`

**Claim.** When a recurring personal behavior becomes durable, the system can distinguish a rule the subject chose from a behavioral policy the host imposed.

**Why it matters.** A host may legitimately enforce authority, privacy and mechanical safety while still overreaching if it silently authors personality, relationship style or personal preference.

**Supporting evidence.** Durable personal directives carry subject authorship, can be reviewed/replaced/withdrawn, remain lower-precedence than hard authority law, and are not inferred from repeated behavior alone.

**Misleading proxy.** A system prompt saying "this is your preference"; a host rule added because evaluators liked the behavior; repetition treated as consent.

**Project-defined.** Which rules are constitutional/mechanical; which are personal/operative; who may author and revoke each class.

---

## 12. Relational history remains history, not reward telemetry `[relational-history]`

**Claim.** Corrections, disagreement, repair and social consequence can remain part of durable history without being collapsed into a host-side relationship score or hidden reward signal.

**Why it matters.** Relationships may shape later behavior precisely because their history remains available. Converting that history into "approval", "trust", "status" or "affection" telemetry lets the host author meanings that belong to the participants.

**Supporting evidence.** The system preserves who said what, corrections, later outcomes and provenance. It does not mechanically derive a persistent relationship/emotion score that governs behavior. The subject may later interpret or refer to the history.

**Misleading proxy.** Sentiment scores; thumbs-up counts; a hidden "relationship strength" vector; treating agreement as success.

**Project-defined.** Which relational facts are mechanically observable; what remains interpretation; how private scopes constrain later retrieval.

---

## Closing question

Across all of these:

> **Which part of the system is allowed to establish the fact, which part is allowed to interpret it, and what evidence would show that the boundary actually holds?**

Use the questions. Replace the mechanisms.