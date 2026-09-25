# claim-harness

A small harness for people building persistent agents (or any system that
keeps state, acts on the world and is supposed to be trustworthy about it).
It lets you:

1. write down what your project **claims** is true;
2. bind each claim to the **executable evidence** that would establish it
   (pytest tests);
3. read what actually **ran** from the test run's JUnit XML;
4. produce a **ledger** that says, for every claim, exactly what the evidence
   supports, and nothing more.

It also ships a list of portable questions ([QUESTIONS.md](QUESTIONS.md)),
small generic property checks for them, and a toy agent that exercises all of
it end to end.

> **Use the questions. Replace the mechanisms.**
>
> The questions ("can replay duplicate a side effect?", "can text grant
> authority?") carry over between projects. The answers, meaning your
> storage, your authority model and your transport, stay your own. Nothing
> here asks you to adopt another project's architecture, schemas or
> vocabulary.

The central rule:

> A positive claim is established because its declared evidence actually ran
> and entailed the claim, not because prose says it passed.

## Quick start

These commands need Python 3.9 or newer, a POSIX shell (macOS or Linux), and a
package index for the one-time install of `pytest`. After the install,
everything runs offline. Run them from this directory, the one that holds
`pyproject.toml`.

### 1. Create a fresh virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install the package

```bash
python -m pip install ".[test]"
```

### 3. Run the toy agent's tests and write JUnit XML

```bash
python -m pytest examples/toy_agent/tests -p claim_harness.network_guard -m "not slow" --junitxml=out/toy-junit.xml
```

`-p claim_harness.network_guard` makes any attempt to reach a non-loopback
network fail the run. `-m "not slow"` deliberately leaves out one test, which
the ledger will then report honestly (see step 4).

Expected: `37 passed, 1 deselected`.

### 4. Build the claim ledger

```bash
python -m claim_harness run --inventory examples/toy_agent/inventory.json --bindings examples/toy_agent/bindings.json --junit out/toy-junit.xml --output out/toy-ledger.json
```

Expected summary line and exit status:

```
summary: PASS=14  FAIL=0  NOT_ATTEMPTED=1  LIVE_ONLY=1  DISPOSITION=1  WITHDRAWN=1
exit 0: no reproducible claim failed
```

`PERSISTENCE-1` is `NOT_ATTEMPTED 2/3`. It requires three tests, and the
process-kill test was deselected in step 3. Two passing tests out of three
required do not make a PASS.

### 5. Inspect the results

```bash
python -m claim_harness show out/toy-ledger.json
python -m claim_harness claims --inventory examples/toy_agent/inventory.json --bindings examples/toy_agent/bindings.json
```

`show` prints every claim with its status, its reason and each piece of
evidence with its executed outcome. `claims` validates the inventory and
bindings and lists which evidence each claim requires. The full record is
`out/toy-ledger.json`. It holds the inputs and their sha256s, the JUnit run
identity and suite properties (including whether the network guard was
active), the code state, and per-evidence outcomes, messages and test-file
hashes.

To establish `PERSISTENCE-1` as well, run the evidence you left out:

```bash
python -m pytest examples/toy_agent/tests -p claim_harness.network_guard --junitxml=out/toy-full-junit.xml
python -m claim_harness run --inventory examples/toy_agent/inventory.json --bindings examples/toy_agent/bindings.json --junit out/toy-full-junit.xml --output out/toy-full-ledger.json
```

Expected: `38 passed`, then `PASS=15  FAIL=0  NOT_ATTEMPTED=0`.

### 6. Watch a claim fail for the right reason

The toy agent has one planted defect. `!digest` records its reply in the
agent's history but never delivers it to the person who asked.

```bash
python -m pytest examples/toy_agent/deliberate_failure -p claim_harness.network_guard --junitxml=out/failing-junit.xml
python -m claim_harness run --inventory examples/toy_agent/deliberate_failure/inventory.json --bindings examples/toy_agent/deliberate_failure/bindings.json --junit out/failing-junit.xml --output out/failing-ledger.json
python -m claim_harness show out/failing-ledger.json
```

Expected: pytest reports `1 failed, 1 passed` and exits 1. The ledger reports
`FAIL DIGEST-1 1/2` and exits 1. `show` gives the reason: `backend reported
reply 'digest: 1 note(s); latest: meet at noon' but 'owner' received []: not
delivered`. The same ledger reports `AMBIGUOUS-1` as `NOT_ATTEMPTED 0/1`,
because its evidence exists but was not part of this run.

Broken accounting is refused outright:

```bash
python -m claim_harness run --inventory examples/toy_agent/deliberate_failure/inventory.json --bindings examples/toy_agent/deliberate_failure/bindings_invalid.json --junit out/failing-junit.xml --output out/invalid-ledger.json
```

Expected: exit 2, `ACCOUNTING INVALID - no ledger written`. It lists an
unknown test, a duplicate binding, a binding for a claim that does not exist,
and an executable claim left with no evidence.

### 7. Reset and rerun

```bash
rm -rf out
python -m pytest examples/toy_agent/tests -p claim_harness.network_guard -m "not slow" --junitxml=out/toy-junit.xml
python -m claim_harness run --inventory examples/toy_agent/inventory.json --bindings examples/toy_agent/bindings.json --junit out/toy-junit.xml --output out/toy-ledger.json
```

The toy agent keeps all its state in per-test temporary directories, so
deleting `out/` is a complete reset. The rerun gives the same claim statuses.

The package's own tests:

```bash
python -m pytest tests -p claim_harness.network_guard
```

## Result states

| State | Meaning | Fails the run? |
|---|---|---|
| `PASS` | Executable claim; **all N** required evidence items ran and passed (M == N). | no |
| `FAIL` | Executable claim; at least one required item ran and failed or errored (including a test module that failed to import). | **yes** (exit 1) |
| `NOT_ATTEMPTED` | Executable claim; nothing failed, but some required item was skipped, deselected or absent from the run (M < N). | no, unless `policy.not_attempted_fails_run` is `true` |
| `LIVE_ONLY` | Observed in a live setting only (a real user, a production incident). Recorded, not reproduced, never PASS. | no |
| `DISPOSITION` | Settled by an owner/project decision (accepted risk, out of scope). A decision, not execution evidence. | no |
| `WITHDRAWN` | No longer claimed. Its old tests may still run and are shown for history, but it never becomes PASS or FAIL. | no |

An invalid inventory, invalid bindings or ambiguous evidence stops everything
with **exit 2**, and no ledger is written.

The rules, in short:

- Missing evidence is not positive evidence. A deselected, skipped or never-run
  test leaves the claim `NOT_ATTEMPTED`.
- `NOT_ATTEMPTED` is not `FAIL`, and `FAIL` is not `NOT_ATTEMPTED`. A failure is
  counter-evidence; a gap is only a gap. If one item fails and another did not
  run, the claim is `FAIL`.
- Live-only evidence is not offline evidence. Owner disposition is not
  execution evidence. Withdrawal is not `PASS`.

## The claim model

**Inventory** (`inventory.json`) lists the claims. Each has a stable `id`, a
human-readable `statement`, an `evidence` kind (`executable`, `live_only` or
`disposition`), and optionally `question` (a slug from QUESTIONS.md),
`withdrawn: {"reason": ...}` and `notes`. A `live_only` claim needs a
`live_record` (`observed`, `summary`); a `disposition` claim needs a
`disposition` (`decided_by`, `decision`, `rationale`). See
[examples/toy_agent/inventory.json](examples/toy_agent/inventory.json).

**Bindings** (`bindings.json`) map each executable claim to the pytest node IDs
it requires, relative to your pytest rootdir. Validation enforces:

- every bound claim exists and is executable (live-only and disposition claims
  cannot take bindings);
- every non-withdrawn executable claim has at least one binding;
- no node ID appears twice for the same claim (one test may support several
  claims);
- every bound test file exists and defines the bound test (checked statically
  under `--project-root`), so a typo is an error, not silently missing evidence.

**Evidence** is the JUnit XML that `pytest --junitxml` writes. The harness
does not import or hook pytest to read it. If the same test appears more than
once across the runs you pass in, its result is ambiguous and the run is
refused.

**Ledger** (`--output`) is JSON. It contains the claim states and reasons,
per-evidence outcomes and messages, sha256s of the inventory, bindings, JUnit
files and test files, run identities, and the code state.

**Code state** is optional provenance. If the project root is a git work tree
it records `HEAD` and whether the tree is dirty. Otherwise it records
`UNAVAILABLE` with a reason and carries on. It uses the local `git` binary
only. No hosting service or network is involved.

## Adapting it to your project

**Tests are evidence, not completion.** A green ledger means the bound tests
ran and passed. Whether those tests actually entail the claim is a judgement
your project has to make, and make first. Decide what your owner/product
requirements *mean* before you bind them to evidence. Otherwise you get a
ledger full of `PASS` for claims nobody needed.

1. Read [QUESTIONS.md](QUESTIONS.md). Pick the questions that matter for your
   system and write each as a claim about *your* system in your own words.
   Some claims will only ever be `live_only` or `disposition`. Say so rather
   than dressing them up as tests.
2. Write tests that exercise each claim through your ordinary paths. Where it
   helps, implement the few adapter interfaces in
   [claim_harness/interfaces.py](claim_harness/interfaces.py) as thin
   wrappers over your own objects, and call the generic checks in
   [claim_harness/checks.py](claim_harness/checks.py). You can also write your
   own assertions; the ledger only cares what ran.
3. Bind each claim to *every* test it needs. If a claim needs a restart test
   and a replay test, bind both. The ledger will not let one stand in for two.
4. Run your tests with `--junitxml`, then `python -m claim_harness run`. Treat
   exit 1 as a failed claim and exit 2 as broken bookkeeping.
5. Try to break it. Keep at least one deliberately broken variant per check
   you rely on (see `examples/toy_agent/broken.py` and
   `tests/test_counterexamples.py`), so you know the check can fail.

Interfaces (all `typing.Protocol`; implement only what your claims need):

| Interface | Method | Observes |
|---|---|---|
| `Runner` | `submit(item) -> outcome` | the ordinary input path |
| `HistoryReader` | `events()` | canonical, persisted history |
| `Lifecycle` | `reopen()` | a fresh instance from persisted state only |
| `SideEffectTransport` | `attempts()` | side-effect attempts as seen at the boundary |
| `Policy` | `allows(principal, capability)`, `can_receive(destination)` | mechanically established authority |
| `DeliveryObserver` | `delivered(subject)` | what actually reached the subject |

The record shapes these return, such as `kind` on outcomes and
`input_id`/`caused_by` on events, are documented at the top of
`interfaces.py`. An adapter maps your records onto them; your own schema stays
as it is.

## The toy agent

`examples/toy_agent/toy_agent.py` is a deliberately small persistent agent,
about 300 lines with no dependencies. It is not a design to copy. It has:

- canonical inputs recorded in an append-only JSONL history before processing;
- commands `!help`, `!note`, `!recall`, `!digest` and `!send`. Anything else
  gets a typed lawful null (`{"kind": "null", "reason": ...}`);
- grants and destination allow-lists changed only by operator methods, never
  by input text;
- two scopes (`alpha`, `beta`);
- a write-ahead outbox, keyed by input, in front of a fake external transport
  whose answer can be `confirmed`, `refused` or `ambiguous`. It never resends,
  and never retries an unknown outcome;
- revocation of destinations and capabilities;
- a delivery channel that records what each subject actually received;
- `reopen()` from disk, with crash injection (`after_accept`, `after_send`) to
  test recovery.

`examples/toy_agent/broken.py` holds one-defect variants: duplicate side
effect on replay, authority taken from text, automatic retry of an ambiguous
send, backend success without delivery, and lawful null reported as failure.
`tests/test_counterexamples.py` shows each one caught by the same check the
real claim relies on. Missing required evidence is demonstrated in step 6.

## What this is not

It is not a framework to build agents with, a policy engine, a theorem prover,
or a certification. It does not know whether your tests are good. It only
refuses to let the ledger say more than your executed evidence does. The
question list is not exhaustive.

## Provenance and license

The claim-accounting pattern and the questions were extracted conceptually
from the reference test harness of ANAXI, a persistent-agent research project,
where they were used to keep completion claims honest. This package is a new,
independent implementation. It contains no code, data, tests or history from
that project and does not need it. See [NOTICE](NOTICE).

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
