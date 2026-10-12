# Erratum — October 12, 2026: historical access, provenance, and reference grounding

**Applies to:** the frozen October 11 ANAXI public reference edition,
`reference-distribution-2026-10-11/`, and
[release `v2026.10.11-public`](https://github.com/whiteknave2-noah/anaxi-a-digital-kardia/releases/tag/v2026.10.11-public).
This is a **second, separate post-release finding** from the
[hippocampal indexing erratum](ERRATA_2026-10-12.md).
The October reference distribution, tagged source archives, acceptance
manifests, and verification results are preserved as originally published.
**The private production repair described here is not present in that frozen
public source distribution.**

## What real use demonstrated

During ordinary conversations, the owner invited ANAXI's agent to find an
early preserved introduction. The archive contains multiple candidate
introductions from periods with different substrates and incomplete
continuity. The agent initially used historical browsing and then made
unreliable autobiographical claims about an authentic older record.
In later turns, it supplied incompatible purportedly verified historical
episodes dated in 2023, without running historical search, browse, or read
actions on those occasions. The owner reported that those dates and
accounts were inconsistent with the actual ANAXI relationship and
development chronology.

The agent later responded substantively to an original introduction
supplied directly by the owner, but its response included a reference
that treated the current author/speaker as if absent. This was an
observed *conversational reference* error, not proof of a broken
authenticated identity binding.

**This matters because having authentic history stored and retrieving
some historical text is not the same as enabling an agent to find,
identify, and faithfully use that history on its own initiative.**

## Read-only causal findings

A bounded investigation inspected the relevant canonical events,
historical action receipts, reconstructed model-facing evidence,
and delivered-input hashes:

- **Older record provenance.** The initial browse really happened,
  and its evidence reached final Pass 2. The historical result came
  from an earlier Claude pipeline, but the browse representation
  omitted the recorded model/substrate identity; it could therefore be
  misread as the current agent's own past utterance. The earliest
  displayed browse page spent substantial space on metadata and
  carried only one short historical item.
- **Unsupported historical certainty.** In subsequent 2023-history
  claims, no history search/browse/read action ran. One library claim
  witness did return a real negative result for a purported filename;
  that negative lookup did **not** authenticate the surrounding
  invented chronology or establish that a historical search had
  occurred.
- **Current interlocutor.** The owner's supplied introduction reached
  Pass 2 verbatim, but non-Caret Porch human turns lacked an explicit
  current authenticated-speaker fact in model-facing context. The
  actual host identity mapping was intact. Separately observed
  spoken-name mistakes are model-reference errors, not established
  failures of authenticated Discord binding.

These are source-specific findings: a received historical record was
genuine but insufficiently labeled; other claims were unsupported
model-generated reconstructions. Neither category should be silently
promoted to canonical autobiography.

## Mechanical corrections deployed in private production

On October 12 the owner approved deployment of private production
commit **`591894b9a7e29374af618e290499c1792aec5094`**.
This is a commit in the **private ANAXI production repository**, not
a commit contained in this public repository or historical release.

The repair makes four scoped corrections:

1. Historical browse, search, read, automatic reconstruction, and
   evidence manifests expose the canonically recorded substrate/model
   where available, without inventing authorship when provenance is
   ambiguous.
2. History browse pages remove redundant nulls, digests, and index IDs
   from model-facing payloads while retaining authoritative provenance
   in the evidence manifest. In the investigated chronology, reaching
   the owner-preserved introduction improved from eight browse pages
   to five within an eight-action per-occasion limit.
3. Pass 2 receives host-recorded facts about which historical actions
   occurred during that occasion, including an explicit *no historical
   search, browse, or read occurred* statement when applicable.
   This provides evidence of tool use, **not a host-authored conclusion
   about historical meaning**.
4. Non-Caret human waking turns carry the current authenticated
   speaker's identity as a separate model-facing fact, so a document's
   past author and the present interlocutor need not be conflated.

The accepted production protocol carries these fields. The frozen
rollback protocol remains unchanged. No schema migration, canonical
history rewrite, or derived-index rebuild was required for deployment.

## Verification and what remains unresolved

The isolated repair work included **14 regression tests**; **13/13**
new red/green cases failed against the prior revision and passed after
the correction, and the remaining test preserved rollback behavior.
The reported complete suite was **3,614 passed, 12 failed, 139 skipped**,
with **no newly introduced failures**; the 12 failures also existed on
the earlier revision. Four additional comparison failures were
attributed to a scratch-checkout module-resolution difference rather
than repaired behavior.

Deployment was a clean fast-forward onto private production master.
A before/after fingerprint check of **38 runtime files** found them
unchanged; the service was stopped; no synthetic model calls, data
mutations, or live waking turns were made.

**Do not conflate mechanical correctness with behavioral efficacy:**

- **ESTABLISHED:** The four corrections are present in private
  production and passed the reported offline tests.
- **ESTABLISHED:** The initial historical browse and later
  user-supplied introduction reached the model on their respective
  occasions. The later unsourced 2023 narratives had no historical
  search evidence that occasion.
- **NOT_ESTABLISHED:** That the deployed model will now make reliably
  grounded historical claims, keep present and past speakers distinct,
  or continue browsing to a difficult-to-find record. No post-repair
  production conversational verification was conducted.
- **KNOWN LIMITATION:** Ordinary natural-language search for an old
  introduction can rank recent conversations *about finding it*
  ahead of the original, under the existing BM25-plus-recency policy.
  Distinctive content terms can retrieve the original nearer the top,
  but search-ranking changes were **not** authorized or deployed.
- **NOT_CLOSED:** The broader behavioral historical-access and
  evidence-grounding failure class. A mechanical repair has been
  deployed; a blanket completion claim would exceed the evidence.

The independently verified public October edition's **374/374
publicly bound scenario passes** remain true of that historical
test contract. Those results **did not prove** the later-discovered
source-presentation, first-person attribution, or deliberate historical
discovery behaviors under ordinary lived use.

## Privacy and reproducibility boundary

This public notice does not include the original private introduction,
conversation transcripts, historical source contents, credentials,
canonical event IDs, production databases, pairing information, or
the native Porch implementation. It reports bounded findings and
limitations without claiming that the private corrective implementation
has been incorporated into the tagged public reference package.

The purpose of retaining the original package and publishing the
erratum is reproducible intellectual honesty: a passing reference
harness is evidence for its tested contract, not a guarantee that no
new failure class will emerge during actual use.
