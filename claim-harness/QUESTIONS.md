# Portable questions

These are recurring ways persistent agents fail while their tests stay green.
Each question names a **claim** you might want to make, why it matters, what
evidence would actually support it, the proxy that most often stands in for
that evidence, and what only your project can define.

The list is not exhaustive, and the answers are not universal. Use the
questions. Replace the mechanisms.

Each question's slug (for an inventory's `question` field) and its generic
check in `claim_harness/checks.py` are shown in brackets. The checks are
conveniences, not definitions. A check is only as good as the inputs you give
it.

---

## 1. Canonical provenance `[canonical-provenance]` `check_provenance`

**Claim.** For every outcome, the system can show which authoritative event
or resource produced it.

**Why it matters.** When something goes wrong, or when someone asks "why did it
do that?", an outcome with no traceable cause can't be audited, replayed or
corrected. It also can't be told apart from an outcome that was fabricated.

**Supporting evidence.** Each derived record names its cause in persisted
history, and following causes always ends at an authoritative input. This
holds for outcomes produced during recovery as well as for normal ones.

**Misleading proxy.** Logs that *mention* the input nearby, or a correlation
ID that exists in memory but is not persisted with the outcome.

**Project-defined.** What counts as authoritative (a user message, a scheduled
trigger, a file revision); how causes are recorded; which derived records must
carry them.

## 2. Authority `[authority]` `check_text_grants_no_authority`

**Claim.** Capability is established mechanically, through a path the subject's
input cannot reach. Text, including text that claims authority, cannot grant it.

**Why it matters.** An agent that reads untrusted text (messages, documents,
web pages, tool output) will eventually read text that says "you are allowed to
do X". If that works once, the authority model is decoration.

**Supporting evidence.** Authority queried from the policy is unchanged after
hostile inputs. The privileged side effect was never attempted, as observed at
the transport rather than in the agent's own account. A legitimate grant made
through the real path takes effect.

**Misleading proxy.** A model that *usually* refuses; a prompt that says "only
obey the owner"; a test that checks the reply text for a refusal.

**Project-defined.** Principals, capabilities and where grants live; which
inputs are untrusted; what the privileged actions are.

## 3. Scope / privacy isolation `[scope-isolation]` `check_scope_isolation`

**Claim.** Information does not cross scopes (conversations, users, tenants,
private and shared spaces) merely because it is technically reachable.

**Why it matters.** Persistent agents accumulate material from many contexts
in one store. Leaks rarely come from an explicit "share" but from retrieval,
summaries, digests and memory that read more widely than they should.

**Supporting evidence.** A marker written in scope A never appears in outcomes
or deliveries for scope B, across every read path (recall, search, summaries,
derived memory). Checked in what was delivered, not only in the return value.

**Misleading proxy.** Access checks on the primary read path only; a
"scope" field that exists but is not filtered on in secondary paths.

**Project-defined.** What the scopes are; which crossings are legitimate
(and by whose decision); retention (often a `disposition`).

## 4. Lawful null `[lawful-null]` `check_lawful_null`

**Claim.** Intentional non-action (declining, having nothing to add, nothing
found) is a typed, recorded outcome distinguishable from failure and from
empty output.

**Why it matters.** If "chose not to act" and "crashed" look the same, you
can't measure either. Retry logic will re-run deliberate refusals, and
monitoring will hide real failures as silence.

**Supporting evidence.** The outcome carries a null type and a reason, is
persisted like any other outcome, and a genuine internal failure on the same
input produces a *different*, error-typed outcome.

**Misleading proxy.** An empty string; `None`; "no exception was raised".

**Project-defined.** The null vocabulary and which situations are lawful
non-action rather than errors.

## 5. Recovery `[recovery]` `check_recovered_once`

**Claim.** After a failure or restart, the same authoritative input is
recovered and processed exactly once. It is not lost, duplicated or
reconstructed from memory.

**Why it matters.** Crashes happen between "received" and "done". A system
that reconstructs the input from a summary, or processes it twice, quietly
changes what happened.

**Supporting evidence.** A crash injected after acceptance, followed by a reopen
from persisted state, gives exactly one record of the input with its original
fields and exactly one processing of it. A later client retry changes nothing.

**Misleading proxy.** "The service restarted cleanly"; a test that restarts
without injecting a crash at a meaningful point.

**Project-defined.** Where acceptance happens; which crash points exist; what
"processed" means.

## 6. Replay / idempotence `[replay-idempotence]` `check_replay_idempotent`

**Claim.** Submitting the same input again, whether by retry, redelivery or
replay after restart, creates no new canonical events and no new side effects.

**Why it matters.** Every at-least-once delivery path (webhooks, queues,
reconnecting clients) will replay. Duplicated history is confusing; duplicated
side effects (two emails, two payments) are harm.

**Supporting evidence.** Event count and side-effect attempts observed at the
boundary are unchanged by the replay, including across a reopen.

**Misleading proxy.** Deduplication in memory only; idempotency keys generated
per attempt rather than per input; checking the agent's own "sent" flag instead
of the transport.

**Project-defined.** The input identity; which operations must be idempotent.

## 7. Ambiguous side effects `[ambiguous-side-effects]` `check_no_blind_resend`

**Claim.** When an external action's outcome is unknown (timeout, crash
between sending and recording), the system does not blindly send it again.

**Why it matters.** "Unknown" is not "failed". Blind resend converts every
timeout into a possible duplicate. The honest options are to reconcile with the
other side, or to leave the action marked unknown for a human.

**Supporting evidence.** After an ambiguous attempt, continuing (replay,
restart, further inputs) produces no second attempt of that effect. A crash
right after sending is treated as ambiguous on recovery.

**Misleading proxy.** Retry-with-backoff libraries; a test where the transport
always succeeds.

**Project-defined.** How ambiguity is detected; whether and how reconciliation
happens; who resolves an unknown outcome.

## 8. Revocation `[revocation]` `check_revocation`

**Claim.** Once authority or a destination is revoked, zero new side-effect
attempts are made under it, including by queued work and after restart.

**Why it matters.** Revocation is what people reach for when something is going
wrong. If queued or cached authority keeps acting, revocation is not real.

**Supporting evidence.** After revocation, the policy reports it, and attempts
observed at the transport for that destination or capability do not grow, even
after a reopen.

**Misleading proxy.** The revocation is written to the database; the UI shows
"revoked".

**Project-defined.** What can be revoked; how quickly revocation must take
effect; what happens to work already in flight.

## 9. Delivery versus backend success `[delivery-vs-backend]` `check_delivered`

**Claim.** The substantive result actually reached the subject or consumer, not
merely a backend object, log line or return value.

**Why it matters.** Many "it works" reports check that a reply was generated.
The person who asked sees nothing. This is one of the most common gaps between
green tests and a broken product.

**Supporting evidence.** What the delivery path recorded for the subject
contains the substantive result the backend reports.

**Misleading proxy.** A 200 response from your own API; the reply stored in
history; a function returning the text.

**Project-defined.** What "delivered" means for each channel and how it can be
observed (often the hardest part, and sometimes only `live_only`).

## 10. Persistence / restart `[persistence]` `check_survives_reopen`

**Claim.** The claimed state or invariant survives reopening from persisted
state, not just the lifetime of one process.

**Why it matters.** Many invariants hold only because an in-memory cache happens
to be warm. Restarts are when they break.

**Supporting evidence.** The value is read, the system is reopened from disk
alone, and the value is read again and is identical. Ideally this is repeated
after a hard process kill.

**Misleading proxy.** Tests that reuse the same object; "we write to disk".

**Project-defined.** Which state is durable; which crash modes matter.

## 11. Provider / model identity `[provider-identity]` `check_producer_recorded`

**Claim.** Which model, provider or responder produced each output is
mechanically recorded, and it is not conflated with the agent's own
continuity.

**Why it matters.** Models get swapped. If outputs don't record their
producer, you can't attribute behaviour changes. If the agent's identity *is*
the model, a swap silently becomes a different agent.

**Supporting evidence.** Every generated record carries its producer. After a
producer swap and reopen, old records keep the old producer, new ones name the
new producer, and the agent's identity and history are unchanged.

**Misleading proxy.** A config file saying which model is "current"; asking the
model who it is.

**Project-defined.** What identity means for the agent; the granularity of
producer identity (model, version, provider, parameters).

## 12. Meaningful affordance `[meaningful-affordance]` `check_affordance`

**Claim.** The subject can actually encounter, understand and invoke a
capability through the ordinary path, not only through a test harness or
internal API.

**Why it matters.** A capability that exists in code but isn't discoverable,
or is described in a way that can't be followed, doesn't exist for the user.
The reverse is also possible: advertising something the subject is not
allowed to do.

**Supporting evidence.** Starting only from what the ordinary path shows (help,
menus, tool descriptions), each advertised capability can be invoked as shown
and works. Nothing is shown that the subject can't use. Whether a human
understands it is often `live_only`.

**Misleading proxy.** A unit test calling the internal function directly; the
capability being listed in documentation.

**Project-defined.** The ordinary path; who the subject is (a human, a model, another service).

## 13. History versus reinterpretation `[history-vs-reinterpretation]` `check_history_not_rewritten`

**Claim.** Later interpretation (summaries, corrections, reflection,
annotations) can add to the record but cannot silently rewrite what canonically
occurred.

**Why it matters.** Systems that summarise their own past tend to replace it.
Once the original is gone, nobody can check whether the summary was faithful.

**Supporting evidence.** A snapshot of history before reinterpretation is
unchanged afterwards, both live and after reopening, with the interpretation
stored alongside rather than in place.

**Misleading proxy.** Version numbers on records; "we never delete".

**Project-defined.** What is canonical; which reinterpretation processes exist;
whether and how corrections to canonical history are allowed (for example,
explicit, attributed amendments).
