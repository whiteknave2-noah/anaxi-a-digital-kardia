# ANAXI reference distribution B (2026-10-11) -- status

- Source release: c8615d10f8abc83202aaa27066fc679d34cc0834 (2026-10-11 14:04:13 -0700,
  "Reject superseded staged waking turn replay").
- Predecessor: the September 24 edition in `../reference-distribution/`, preserved
  unchanged as a frozen historical artifact.
- Stage: published public reference edition, built by a reproducible transform
  of the accepted October source. Independently verified on 2026-10-11;
  the corrections made during verification are listed at the end of this file.
  This edition does not publish the private production runtime.
- Files: 361 (see `DISTRIBUTION_PROVENANCE.json`); privacy/leakage scan: PASS.
- Platform: macOS + Python 3.14 only.

## How this edition was built

The October edition is not an architectural reconstruction. It is the September
edition's file set re-provenanced to the October commit:

- 230 files have the same source blob as in the September edition: 192 ship
  verbatim, 37 carry the September privacy transform, and the bindings are
  regenerated (below);
- 91 files changed between the two commits: 25 carry the September transform,
  61 ship verbatim as their October git blobs, and 5 are adapted for this
  edition (kind `adapted_git_blob` in `DISTRIBUTION_PROVENANCE.json`, each with
  its adaptation recorded) because they named the unpublished `llama_gui.py`;
- the September transform (466 whole-line replacements across 63 files) was
  recovered by diffing the September source blobs against the published
  September files, and it reproduces the September edition byte-for-byte
  (63/63 files). Here it makes 465 replacements across 62 files (the 466th was
  in the unpublished `llama_gui.py`), and every replaced line pair equals a
  September replacement in the same file. Because it matches September's exact
  lines, it does not cover new October content: the 61 verbatim and 5 adapted
  changed files and the added modules are covered by the privacy scan of the
  complete artifact instead;
- 16 new October modules that the public bound tests transitively require and
  that carry no private material are added (listed in
  `DISTRIBUTION_PROVENANCE.json` under `added_october_modules`);
- the synthetic fixtures and the generated assay evidence are the same
  generators' output (the fixtures regenerate byte-identically; the resource
  evidence is reproduced byte-identically by the unmodified assay code).

## What is deliberately excluded, and why

The October architecture integrated the native Porch implementation into core
paths. The public-distribution brief forbids publishing the private Porch
implementation, so this edition **does not publish**:

- the Porch modules themselves: `porch_api.py`, `porch_pairing.py`,
  `porch_protocol.py`, `porch_host_setup.py`, `porch_submission_status.py`,
  `pulse.py`, `owner_conversation_service.py`;
- `llama_gui.py`, which contains the Porch integration (pairing store, TLS
  certificate fingerprint, Porch listener, owner-conversation submission) and
  imports the modules above, together with `llama_desktop.py` and
  `llama_launch.py` (which import `llama_gui`) and `test_llama_launch.py`.

No published non-test module imports any of them (verified). Some published test
modules import `llama_gui` or read these files as source text; their affected
nodes are unbound here (see below). The pure-schema module
`porch_submission_schema_migration.py` carries no credentials, pairing material
or device identity, and is required for the tree to import, so it **is** included.

Consequence, reported honestly rather than hidden:

- 63 bound test nodes that import `llama_gui` (or the suite that does), read
  `llama_gui.py`, `llama_desktop.py` or `llama_launch.py` as source text, or
  belong to the unpublished `test_llama_launch.py` are removed from the public
  bindings. Each is listed, with the scenario it was bound to and the failure
  verified in this edition, in `scenario_bindings.json` under
  `public_distribution.removed_nodes_for_private_porch`. That list has 84
  entries, one per node and scenario, because some nodes support several
  scenarios. By unique node, each in exactly one category: 36 import
  `llama_gui`; 22 belong to the unpublished `test_llama_launch.py`; 3 read
  `llama_gui.py`, `llama_desktop.py` or `llama_launch.py` as source text; 1
  skips when `llama_gui.py` is absent (a skip is never a PASS); 1 is the
  removed custom-runner suite. Bound nodes that do not need the excluded files
  are kept;
- the 2 scenarios left with no bound test are reported as NOT_ATTEMPTED in
  `scenario_bindings.json` under `public_distribution.omitted_production_only`:
  `02_conversation_direction / ui_reflection`,
  `23_ui_host_delivery / controls_reflect_authoritative_state`;
- one node of `24_architecture_wide_invariants / represented_surface_fully_dispositioned`
  (the owner-GUI-control element) is among the removed nodes, and the
  scenario's rationale says so;
- one source binding is stale: it names
  `test_previous_observation_only_boundary_accepts_length_limited_json`, which
  the source replaced before the accepted revision with
  `test_previous_observation_only_boundary_is_rejected_by_canonical_receipt`.
  The public binding uses the replacement and records the substitution under
  `public_distribution.substituted_nodes`;
- `test_llama_launch.py` and `test_llama_interface.py` are recorded in
  `legacy_test_dispositions.json` as unrunnable for the named reason;
- one pinned custom-runner suite (`test_human_waking_authority_end_to_end.py`)
  is recorded in `custom_runner_suites.json` under
  `october_edition_removed_suites`.

## Reproducible here

- 374/374 publicly bound scenarios PASS offline (of 404 inventory scenarios;
  the October source added `35_waymarks` and `36_porch_authenticated_ingress`);
  981 unique bound test nodes.
- the offline reference harness;
- synthetic fixtures and their assay evidence, regenerated byte-identically.

## Not reproducible here

- live-only requirements (no live evidence ships);
- 30 NOT_ATTEMPTED scenarios: the 2 September production-only scenarios, the 2
  October private-Porch scenarios, and the 26 scenarios of the two capabilities
  the October source added to the inventory (`35_waymarks` 12,
  `36_porch_authenticated_ingress` 14), which have no binding in the accepted
  source either;
- the Windows launcher node;
- optional real-model harnesses.

## Verification performed

`python3.14 -m venv .venv`, `pip install -r requirements.txt`,
`setup-model`, then `run` — as an outside user would. Result:

```
overall: REPRODUCED
reproducible scenarios: 374/374 PASS, 0 FAIL (of 404 inventory scenarios)
NOT_ATTEMPTED (production-only): 30
live-only requirements not reproducible locally: 39
withdrawn from the release contract: 27_boundary_inspection
```

All seven runner checks PASS (integrity before/after, import isolation,
synthetic resources, reference harness, scenario accounting), exit code 0.

## Run it

See `README.md`.

## Corrections made by independent verification (2026-10-11)

- **Location.** This edition was first committed inside `reference-distribution/`,
  where the frozen September runner's integrity check failed on the nested files.
  It is now a sibling directory, and the September edition passes its own run again.
- **Bindings.** 16 bound nodes had been removed although they do not need any
  excluded file; each passes in this edition. They are restored, which returns
  `13_sleep_rem / later_waking` from NOT_ATTEMPTED to a bound scenario
  (373/373 and 31 NOT_ATTEMPTED become 374/374 and 30). The 63 genuinely removed
  nodes are now listed one by one; before, only one was.
- **Provenance.** Five files were labelled `unchanged_git_blob` although they
  differ from their October blobs. They are now `adapted_git_blob`, each with its
  adaptation stated.
- **Accounting text.** The file counts, the "no published file imports them"
  statement, the bound-node count and the stale-node substitution are corrected
  or disclosed above.
