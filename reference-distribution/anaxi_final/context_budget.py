"""OWC9: aggregate waking context-budget authority.

Root cause this module repairs (spec context): multiple independently-
bounded context contributors (OWC2/OWC8 dialogue window, WSP2-P3 Bridge
C episode context, WSP2-P4 ACTIVE_WORKSPACE_CONTINUITY_V1, Kardia/
memory, the current human message, Pass-specific task instructions)
were composed into a single prompt with no authority over their SUM.
OWC8 bounded recent dialogue; nothing bounded the COMPLETED prompt. A
live production turn measured prompt_eval_count=4092 against a 4096-
token effective ceiling, leaving 4 tokens of generation room -- the
model emitted `{"act": "` and was cut off by `done_reason: "length"`.
Correctly contained as MALFORMED_ACT by the existing validator, but the
call should never have been sent in that shape.

GOVERNING INVARIANTS (verbatim from the OWC9 gate spec):
  A. Generation reserve is allocated first.
  B. No model call may be sent with a prompt that consumes the space
     reserved for that call's required generation.
  C. Individually bounded contributors do not get to overcommit the sum.
  D. Context trimming is mechanical, deterministic, and source-aware.
  E. No model call decides what context "matters."
  F. No salience / importance / value heuristic.
  G. Complete units are preferred over torn or semantically corrupted
     units.
  H. Current human message and core control state must not disappear
     silently.
  I. Pass-1 may be leaner than Pass-2, but it must not become blind to
     mechanically relevant context.
  J. Fail closed != fail dead.

CONTEXT_CEILING = 4096 (spec section 3): confirmed via ollama.show()
metadata inspection to be the Ollama RUNTIME DEFAULT, not model-native
-- neither ask_llama_for_json()'s nor call_llama()'s options dict sets
num_ctx, and the installed gemma4:e4b Modelfile's own PARAMETER block
sets none either (only temperature/top_k/top_p). The model's own
manifest (modelinfo['gemma4.context_length']) reports 131072 tokens of
architectural capacity. Left UNCHANGED this gate: this machine's GPU
(nvidia-smi) reports only 4096 MiB total VRAM, already tight for an 8B
Q4_K_M model -- raising num_ctx without a live-inference latency/VRAM
benchmark (explicitly out of scope: "no live model inference") would be
an unvalidated risk on this specific installation. Kept as one named
module constant, never inlined at a call site, specifically so a
future, benchmark-validated increase is a one-line change -- aggregate
budgeting remains authoritative regardless of the ceiling's value.
"""

CONTEXT_CEILING = 4096

# spec section 14: "Do not misuse MALFORMED_ACT for a host-side budget
# failure." A distinct, honest failure code, reused through the EXISTING
# ConversationDirectionFailure/conversation_direction_trace containment
# machinery (narrowest new explicit containment -- no new exception
# class or trace schema required).
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"

# Grounded in a computed worst-case structurally-valid Pass-1 JSON
# control envelope: {"act": "yield_direction" (longest ALLOWED_ACTS
# member), "thread": <200-char topic label>, "direction_request":
# "request_human" (longest VALID_DIRECTION_REQUESTS member),
# "relinquish_direction": false, "background_activity_request":
# "resume_own_pause" (longest VALID_BACKGROUND_ACTIVITY_REQUESTS
# member)} -- measures 360 UTF-8 bytes/conservative-estimator-tokens
# (see estimate_tokens() below). Rounded up with headroom for the
# model's own incidental whitespace/formatting variance.
PASS1_GENERATION_RESERVE = 512

# Grounded in already-logged historical Pass-2 output lengths (195-213
# chars observed across several successful production turns in the
# live audit that preceded this gate) combined with this project's own
# established brevity-steering aesthetic (OWC4-S1's style_instruction:
# "Respond with extreme brevity and precision... Avoid filler"). Roughly
# 3.5x the largest observed healthy reply -- generous enough for an
# occasional longer, more substantive reply without being wastefully
# large against the tight 4096 ceiling. Not frozen from an earlier,
# undocumented discussion value -- derived fresh from the evidence above.
PASS2_GENERATION_RESERVE = 768


class IncompleteCompletionError(Exception):
    """Transport termination is not proof of a complete model response."""

    def __init__(self, failure_code):
        self.failure_code = failure_code
        super().__init__(failure_code)


def require_complete_response(response, generation_reserve):
    """Validate transport completion, never punctuation or semantic meaning.

    The same reserve used for admission bounds decoding. A valid JSON
    envelope cannot override length exhaustion, interruption or missing
    completion evidence. Normal stop at exactly the bound is allowed.
    """
    if response.get("done_reason") == "length":
        raise IncompleteCompletionError("COMPLETION_LIMIT_REACHED")
    count = response.get("eval_count")
    if (response.get("done") is not True or response.get("done_reason") != "stop"
            or type(count) is not int or count < 0):
        raise IncompleteCompletionError("INCOMPLETE_MODEL_COMPLETION")
    if count > generation_reserve:
        raise IncompleteCompletionError("COMPLETION_LIMIT_EXCEEDED")

# Fixed buffer for chat-template/special-token overhead (role markers,
# BOS/turn separators) that summing raw message content bytes does not
# capture. NOT compensating for estimator imprecision -- estimate_tokens()
# is a proven upper bound and can only ever overcount, never undercount
# (see its own docstring) -- this margin is strictly for non-content
# overhead.
SAFETY_MARGIN = 128

PASS1_MAX_PROMPT_BUDGET = CONTEXT_CEILING - PASS1_GENERATION_RESERVE - SAFETY_MARGIN
PASS2_MAX_PROMPT_BUDGET = CONTEXT_CEILING - PASS2_GENERATION_RESERVE - SAFETY_MARGIN

# OWC9-P1: roaming Stage-1 reserve. Grounded in the measured worst-case
# structurally-valid roaming-choice JSON envelope:
# {"roaming_act": "wait_for_human" (longest VALID_ROAMING_ACTS member),
# "wait_minutes": 60} -- measures 53 UTF-8 bytes/conservative-estimator-
# tokens, well under a tenth of PASS1_GENERATION_RESERVE. Deliberately
# NOT reusing the waking Pass-1 reserve (spec section 5 explicitly
# forbids blindly doing so): roaming's schema has only two small fields
# (a bounded enum, a small integer) versus Pass-1's five, one of which
# (`thread`) is an unbounded-ish free-text label. 128 keeps over 2x
# headroom against the measured worst case while staying proportionate
# to the objectively smaller schema.
ROAMING_GENERATION_RESERVE = 128
ROAMING_MAX_PROMPT_BUDGET = CONTEXT_CEILING - ROAMING_GENERATION_RESERVE - SAFETY_MARGIN

# OWC9-P3 Part B: WSP1 supervised-workspace-action reserves. Two
# DIFFERENT values, not one reused blindly, because the two passes'
# output schemas genuinely differ:
#
# WSP1 Pass-1's output ({"resource_class", "action", "relative_path",
# "content"}) includes `content`, which is GENUINELY UNBOUNDED --
# workspace_capability.append_journal_entry() enforces no maximum
# length on a journal-append's text (confirmed by source audit). 1024
# gives real room for a substantive multi-paragraph journal entry
# (well beyond ordinary waking's ~200-char observed Pass-2 replies)
# while still leaving the measured ~2428-byte real-production hard
# floor (identity_preamble + CONVERSATION_MODE_CLAUSE + a typical
# human request + WSP1's own, much smaller task instruction -- unlike
# ordinary waking's, WSP1's task/schema text was measured safe to
# include as HARD, see workspace_supervisor.py's own Contribution
# construction) room to fit with headroom left over for SOFT
# retrieval/dialogue. No real production history exists yet for this
# narrow, explicitly-supervised pathway to derive an exact observed
# distribution from (unlike PASS2_GENERATION_RESERVE's own 195-213
# char basis) -- reported as a judgment call, not a measurement.
WSP1_PASS1_GENERATION_RESERVE = 1024
WSP1_PASS1_MAX_PROMPT_BUDGET = CONTEXT_CEILING - WSP1_PASS1_GENERATION_RESERVE - SAFETY_MARGIN

# WSP1 Pass-2's output ({"expression": "<natural-language reply>"}) is
# IDENTICAL in shape to ordinary waking Pass-2's own schema -- Clark
# narrating what happened, not writing unbounded content -- so reusing
# PASS2_GENERATION_RESERVE here is a justified match, not a blind
# reuse (spec explicitly permits reuse when the schema genuinely is
# the same).
WSP1_PASS2_GENERATION_RESERVE = PASS2_GENERATION_RESERVE
WSP1_PASS2_MAX_PROMPT_BUDGET = CONTEXT_CEILING - WSP1_PASS2_GENERATION_RESERVE - SAFETY_MARGIN

# OWC9-P4 section 5: Sleep/REM's own model-specific budget profile.
# Sleep/REM runs on llama3.2:3b, NOT gemma4:e4b -- explicitly NOT
# assumed to share Gemma's own CONTEXT_CEILING by analogy. Confirmed
# independently via a read-only, non-inference ollama.show("llama3.2:3b")
# metadata inspection (same method CONTEXT_CEILING's own comment above
# used for Gemma): this model's own Modelfile sets no num_ctx PARAMETER
# override either, so it relies on the SAME Ollama-server runtime
# default (4096) -- a coincidental match confirmed by direct metadata
# check, not an assumption. If this model's serving configuration ever
# changes to set an explicit num_ctx, this constant must be re-derived
# the same way, independently -- never silently inherited from Gemma's.
SLEEP_LLAMA_CONTEXT_CEILING = 4096

# REM consolidation's own output (upsert_nodes/add_edges/delete_nodes)
# can genuinely describe MULTIPLE memory-graph entries in a single
# response -- structurally larger than any other schema in this
# project. No live inference was authorized to measure this gate
# (spec section 5: "No live inference is required"), so this is an
# explicit judgment call, not a measurement -- chosen generously above
# WSP1 Pass-1's own 1024-byte reserve for exactly that reason.
SLEEP_REM_GENERATION_RESERVE = 1536
SLEEP_REM_MAX_PROMPT_BUDGET = SLEEP_LLAMA_CONTEXT_CEILING - SLEEP_REM_GENERATION_RESERVE - SAFETY_MARGIN

# Reflection's own output (evolution_choice + updated_kardia's four
# short string fields + constitutional_argument) is a single, modestly
# sized record -- comparable in shape to ordinary waking Pass-2's own
# reserve, so reusing that same value here is a judgment call grounded
# in genuine shape similarity, not a blind copy.
SLEEP_REFLECTION_GENERATION_RESERVE = PASS2_GENERATION_RESERVE
SLEEP_REFLECTION_MAX_PROMPT_BUDGET = SLEEP_LLAMA_CONTEXT_CEILING - SLEEP_REFLECTION_GENERATION_RESERVE - SAFETY_MARGIN

# SLP1-B: dormant Sleep Selection's own budget profile. Same llama3.2:3b
# substrate as REM consolidation/identity reflection above, so
# SLEEP_LLAMA_CONTEXT_CEILING is reused directly (already independently
# confirmed for this exact model, not assumed by analogy from gemma4:e4b
# -- see that constant's own comment). Selection's own output shape
# ({"selected_wmu_ids": [...]} , at most MAX_SELECTED_WMUS_PER_CALL=8
# ids, each a fixed "wmu-" + 26 hex chars = 30-char string) is smaller
# and simpler than every other schema in this module -- a full 8-item
# array of quoted 30-char ids plus wrapper punctuation measures roughly
# 290 bytes. No live inference was authorized to measure this gate
# (same discipline as SLEEP_REM_GENERATION_RESERVE above), so 512 is an
# explicit judgment call: comparable in magnitude to PASS1_GENERATION_
# RESERVE's own small-fixed-JSON-schema reserve, generously above the
# ~290-byte computed worst case for headroom against incidental model
# formatting/whitespace variance, without being wastefully large.
SLEEP_SELECTION_GENERATION_RESERVE = 512
SLEEP_SELECTION_MAX_PROMPT_BUDGET = SLEEP_LLAMA_CONTEXT_CEILING - SLEEP_SELECTION_GENERATION_RESERVE - SAFETY_MARGIN

# SLP1-C: dormant Sleep Transformation's own budget profile. Same
# llama3.2:3b substrate, same SLEEP_LLAMA_CONTEXT_CEILING.
#
# The mandate's own DEFAULT output envelope (MAX_DERIVATIONS_PER_CALL=4,
# MAX_DERIVATION_CHARS=600) does not mechanically fit this ceiling
# alongside a workable input budget: the fixed neutral-framing +
# schema-instruction prompt text alone already measures ~1250 bytes
# (byte_estimate), and a 4-item worst-case output (4 * (600 chars +
# ~80 bytes of quoted wmu ids + ~40 bytes of JSON punctuation) + outer
# wrapper) measures ~2900 bytes -- reserving that much generation room
# would leave under 900 bytes for input, not enough to even hold the
# fixed instructions once, let alone any candidate material. This is
# exactly the mandate's own anticipated case ("if the actual OWC9
# output envelope requires a smaller safe bound... choose the largest
# smaller safe value; document the mechanical reason; continue"):
# sleep_transformation.py's own MAX_DERIVATIONS_PER_CALL is reduced
# from the mandate's default of 4 to 2 (MAX_DERIVATION_CHARS stays at
# the mandate's own 600). Worst-case 2-item output is then
# 2 * (600 + 80 + 40) + 20 =~ 1460 bytes; GENERATION_RESERVE=1536
# (matching SLEEP_REM_GENERATION_RESERVE's own magnitude) comfortably
# covers it, leaving MAX_PROMPT_BUDGET=2432 -- identical to
# SLEEP_REM_MAX_PROMPT_BUDGET, both derived from the same ceiling and a
# comparably-sized generation reserve.
SLEEP_TRANSFORMATION_GENERATION_RESERVE = 1536
SLEEP_TRANSFORMATION_MAX_PROMPT_BUDGET = SLEEP_LLAMA_CONTEXT_CEILING - SLEEP_TRANSFORMATION_GENERATION_RESERVE - SAFETY_MARGIN


def estimate_tokens(text):
    """Conservative token-count UPPER BOUND, never a casual chars/4
    average (spec section 4 explicitly forbids using chars/4 as a hard
    safety boundary). Uses UTF-8 byte count: every practical byte-level/
    BPE tokenizer vocabulary -- including this model's own llama-style
    tokenizer, per its installed manifest (tokenizer.ggml.model=llama)
    -- includes single-byte fallback tokens, which guarantees
    token_count <= byte_count for ANY input, mathematically, regardless
    of language or content shape. This cannot silently undercount,
    which is the exact property a hard safety boundary requires.

    No exact local tokenizer is available for this specific model
    without downloading additional tokenizer files (out of scope this
    gate: "Do NOT: download models; download packages"); the installed
    `ollama` Python client exposes no tokenize() method. This is
    therefore the narrowest available conservative estimator, not a
    permanent substitute for exact preflight tokenization -- reported
    as such in this gate's own final report.

    In practice this substantially OVERcounts (real English/JSON text
    averages ~3-4 bytes per token), so the compositor below will trim
    somewhat more aggressively than a perfectly-calibrated estimator
    would require -- the safe direction per governing invariant B."""
    if not text:
        return 0
    return len(text.encode("utf-8", errors="replace"))


# CAP2-B: honest modality-specific admission accounting for a
# delivered photograph (spec: "Do NOT treat binary media cost as zero
# or pretend UTF-8 text estimation measures it"). estimate_tokens()'s
# UTF-8-byte-count contract is meaningless for a base64-inflated image
# payload -- it would wildly overcount (base64 text bytes, not real
# vision tokens) or, if a Contribution were built with empty/placeholder
# rendered_text instead, silently undercount to near zero. Neither is
# acceptable, so an image Contribution's `.cost` is instead set
# directly from this measured constant (see build_image_contribution()
# below) -- never derived from rendered_text at all.
#
# Measured via bounded, explicitly-authorized live calibration assays
# against the installed gemma4:e4b/Ollama 0.32.15 (this project's own
# established calibration discipline, see llama_anaxi.py's
# _PASS2_SCAFFOLD_CALIBRATED_COST).
#
# CAP2-B (initial): six 512x512 synthetic PNGs (solid colors + rendered
# text) each measured prompt_eval_count 143-158 (text-only baseline:
# 14). At the time this was read as "cost is materially constant
# regardless of resolution" -- CAP2A's own follow-up assay below shows
# that reading was WRONG; it only ever tested one size.
#
# CAP2A-C (corrected, envelope-wide): cost genuinely SCALES with
# resolution, but PLATEAUS at this model/runtime's own internal
# processing cap -- it does not grow without bound across the accepted
# envelope. Measured prompt_eval_count (fixed 4-token "One word."
# prompt + one image, num_predict=4, temperature=0.0), one square size
# per boundary plus two extreme-aspect-ratio cases:
#   512x512    -> 135
#   1024x1024  -> 270
#   2048x2048  -> 270   (IDENTICAL to 1024x1024 -- confirms a plateau,
#                        not runaway growth, at >=1024px/side; the
#                        model/runtime evidently caps its own internal
#                        working resolution somewhere at or below 1024)
#   2048x16    -> 57    (extreme wide aspect, permitted by
#                        MAX_IMAGE_DIMENSION since neither side exceeds
#                        2048)
#   16x2048    -> 57    (extreme tall aspect)
# Worst observed value anywhere in the accepted envelope
# (0 < width,height <= MAX_IMAGE_DIMENSION=2048) is 270, comfortably
# under IMAGE_ADMISSION_TOKEN_COST's own 512 -- retained UNCHANGED
# (CAP2A option A: "measured cost remains safely <=512 throughout the
# accepted envelope"), now backed by boundary evidence spanning the
# envelope instead of one single size. Not lowered to e.g. 320 despite
# the plateau: 512 already carries genuine headroom (270 -> 512 is
# ~1.9x) against measurement noise and this model's own occasional
# non-determinism (observed in the CAP2 vision-grounding assay), and
# nothing in OWC9's own budget accounting rewards shaving this margin
# further. MAX_IMAGE_COUNT stays 1 (workspace_capability.py)
# specifically so this fixed-per-image constant is never silently
# multiplied.
IMAGE_ADMISSION_TOKEN_COST = 512

# CAP2A-C: the actual worst-case prompt_eval_count observed anywhere in
# the accepted envelope (512/1024/2048 square, plus 2048x16/16x2048
# extreme aspect ratios -- see the measurement table above), preserved
# as its own named, permanently-checked constant so the claim "512 is
# safe throughout the envelope" is a standing regression test
# (test_context_budget.py's own test_image_admission_cost_covers_
# measured_envelope_worst_case), not just a comment that can silently
# drift out of sync with IMAGE_ADMISSION_TOKEN_COST above.
MEASURED_WORST_CASE_IMAGE_PROMPT_EVAL_COUNT_CAP2A = 270


def build_image_contribution(kind=None):
    """CAP2-B/F: builds a HARD Contribution representing one admitted
    photograph's real, modality-specific token cost -- HARD because
    dropping it silently under budget pressure would mean claiming (in
    provenance/narrative) that a photograph was viewed while never
    actually sending its pixels, which this project's own honesty
    invariants forbid. An oversized hard set (this image plus the
    turn's other hard contributions) fails the whole call closed via
    compose_within_budget()'s existing hard-overflow path -- zero
    inference, never a partial/degraded image send.

    `rendered_text` is a short, fixed, human-legible placeholder ONLY
    (never fed to estimate_tokens() for costing -- see module comment
    above); `.cost` is overwritten immediately after construction with
    the calibrated constant."""
    contribution = Contribution(kind or RESOURCE_ENCOUNTER, "[one photograph admitted to this request]", hard=True)
    contribution.cost = IMAGE_ADMISSION_TOKEN_COST
    return contribution


# ============================================================= CAP2E: qwen3-vl:4b
#
# A genuine photograph-VIEW expression is routed to qwen3-vl:4b instead
# of gemma4:e4b (CAP2D-P1 proved gemma4:e4b does not ground on pixels
# on this installation; qwen3-vl:4b does, 12/12 counterbalanced). This
# is a DIFFERENT model with its own measured behavior -- reusing
# gemma4's own constants "because the message shape is similar" is
# exactly the mistake OWC9-P4's own model-specific-profile discipline
# (SLEEP_LLAMA_CONTEXT_CEILING, independently re-derived rather than
# assumed from gemma4:e4b by analogy) already forbids repeating.
#
# CONTEXT CEILING: 4096, independently re-confirmed for qwen3-vl:4b
# specifically (not assumed): `ollama show qwen3-vl:4b` reports no
# num_ctx PARAMETER override in its own Modelfile, and a live `ollama
# ps` after loading it shows CONTEXT=4096 -- the same server-wide
# runtime default gemma4:e4b also relies on, confirmed independently
# rather than inherited by assumption (mirrors CONTEXT_CEILING's own
# comment above and SLEEP_LLAMA_CONTEXT_CEILING's own precedent).
QWEN_VISION_CONTEXT_CEILING = 4096

# GENERATION RESERVE: measured via a small bounded synthetic assay
# (CAP2E), not assumed from PASS2_GENERATION_RESERVE. Two realistic
# JSON-wrapped one-sentence photograph expressions ("{"expression":
# "..."}"), each WITH a real admitted image, completed cleanly
# (done_reason="stop") using 110 and 72 total eval tokens (hidden
# `<think>` reasoning + final content combined -- qwen3-vl:4b's own
# installed build uses RENDERER/PARSER "qwen3-vl-thinking", and
# CAP2D-P1 already found `think=False` does NOT reliably suppress this
# model's own hidden reasoning the way it does for gemma4:e4b: a
# too-small num_predict/reserve there produced EMPTY final content
# because thinking alone exhausted it -- exactly the failure this
# reserve must never reproduce). CAP2D-P1's own no-image control also
# showed a real failure mode distinct from a clean stop: an unresolved
# `<think>` block can run to whatever generation ceiling is given
# without ever concluding (done_reason="length", content still empty).
# 1024 is chosen as a conservative reserve against that risk -- roughly
# 9-14x the two clean observed completions, not merely 2-3x, because
# the failure mode being guarded against is qualitatively different
# (an open-ended reasoning loop) rather than ordinary length variance.
QWEN_VISION_GENERATION_RESERVE = 1024

# Same fixed non-content chat-template-overhead margin convention as
# SAFETY_MARGIN elsewhere in this module -- not re-measured per model,
# since it covers template/role-marker overhead, not model behavior.
QWEN_VISION_SAFETY_MARGIN = 128

QWEN_VISION_MAX_PROMPT_BUDGET = QWEN_VISION_CONTEXT_CEILING - QWEN_VISION_GENERATION_RESERVE - QWEN_VISION_SAFETY_MARGIN

# LIVE FINDING (scanned-PDF recheck, H 01M2XS6DS3KVBKNMBA8T4FAJ7V): with a page image the vision model
# (qwen3-vl:4b on this Ollama build) emits HIDDEN REASONING even with think=False -- 600 to 2300+
# tokens, up to ~10,000 characters -- before the answer. The vision call was sent with no num_predict
# and the 4096 default context, so on one live turn (prompt 2628) the reasoning consumed the entire
# remaining 1468 tokens: done_reason=length, ZERO content, no X (2 of 4 and 1 of 6 measured runs
# failed the same way). The admission ceiling above is unchanged (the PROMPT is still admitted against
# 4096); only the RUNTIME window for the generation is enlarged, so the reasoning has somewhere to go.
# Measured with the real model on the live page image: at 4096 2 of 4 runs and 1 of 6 died empty; at
# 8192/4096 the failures fell to about 3% (a rare runaway reasoning loop that no tested option removed:
# brief-answer instruction, repeat_penalty, temperature 0.2 -- 20 runs each, ~1 loop per 20), and
# successful generations reached 3.8k tokens, so the cap is set at 6144 (window 10240) to keep real
# long-but-finite reasoning from being cut. A runaway loop still ends as a truthful
# COMPLETION_LIMIT_REACHED. qwen3-vl is loaded only for this call, so a larger window does not touch
# the text model's calibrated 4096 profile.
QWEN_VISION_RUNTIME_CONTEXT = 10240
QWEN_VISION_RUNTIME_MAX_GENERATION = 6144
assert QWEN_VISION_MAX_PROMPT_BUDGET + QWEN_VISION_RUNTIME_MAX_GENERATION < QWEN_VISION_RUNTIME_CONTEXT

# IMAGE ADMISSION: qwen3-vl:4b's own real image cost is NOT a flat
# constant like gemma4's IMAGE_ADMISSION_TOKEN_COST -- CAP2E's own
# bounded synthetic assay (512/1024/1536/2048 square + 2048x16/16x2048
# extreme aspect, "One word." text prompt, temperature=0, think=False)
# measured:
#   512x512    -> 1037   (text-only baseline was 11)
#   1024x1024  -> 1037   (IDENTICAL to 512x512 -- a genuine flat
#                         plateau up to this area, not noise)
#   1536x1536  -> 2317
#   2048x2048  -> request REJECTED by the Ollama server itself:
#                 "request (4109 tokens) exceeds the available context
#                 size (4096 tokens)" -- i.e. this exact image, alone,
#                 with a 3-token text prompt, does not fit in this
#                 model's own served context window at all. This is
#                 direct, mechanical proof that this pathway MUST be
#                 able to fail closed on a large admitted photograph,
#                 not silently downsample or truncate it.
#   2048x16    -> 1102   (extreme wide aspect, small true area)
#   16x2048    -> 1102   (extreme tall aspect, small true area)
# The three above-plateau points (1024/1536/2048) fit a genuinely
# linear-in-pixel-area relationship (slope ~0.00097-0.00098
# tokens/px^2, consistent with this vision tower's own reported
# patch_size=16 * spatial_merge_size=2 = 32px effective patch edge,
# i.e. ~1 token per 1024px^2) -- this is a real, mechanistically
# grounded scaling law, not a curve-fit coincidence, so a genuine
# area-aware formula is the honest model of this cost, unlike gemma4's
# own single flat constant (that model plateaus across its ENTIRE
# accepted envelope; this one only plateaus below ~1024x1024).
QWEN_IMAGE_PLATEAU_AREA_PX = 1024 * 1024
# Measured plateau max (1037) plus margin, rounded up.
QWEN_IMAGE_PLATEAU_TOKEN_COST = 1100
# Conservative: the measured real slope is ~1 token per ~1024px^2;
# dividing by a SMALLER number here deliberately overcounts (more
# tokens claimed per unit area than truly observed), which is the safe
# direction for a HARD, budget-gating cost.
QWEN_IMAGE_TOKENS_PER_PIXEL_AREA_DENOMINATOR = 900


def qwen_image_admission_cost(width, height):
    """CAP2E: conservative, measured, area-aware image admission cost
    for the qwen3-vl:4b vision pathway. Deliberately NOT a single flat
    constant (see module comment above) -- a large admitted photograph
    would otherwise be silently undercounted, letting an oversized
    request reach the model instead of failing the turn closed before
    any inference occurs. Always returns an integer upper-bound
    estimate; the caller supplies the image's own real, already-
    measured width/height (see workspace_capability.deliver_photograph_
    bytes()'s own width/height fields) -- this function never guesses
    or re-derives them."""
    area = max(0, int(width)) * max(0, int(height))
    if area <= QWEN_IMAGE_PLATEAU_AREA_PX:
        return QWEN_IMAGE_PLATEAU_TOKEN_COST
    extra = area - QWEN_IMAGE_PLATEAU_AREA_PX
    return QWEN_IMAGE_PLATEAU_TOKEN_COST + -(-extra // QWEN_IMAGE_TOKENS_PER_PIXEL_AREA_DENOMINATOR)  # ceil div, no import needed


def build_qwen_image_contribution(width, height, kind=None):
    """CAP2E sibling of build_image_contribution() -- HARD for the
    identical honesty reason (see that function's own docstring), but
    costed via qwen_image_admission_cost()'s genuine area-aware formula
    instead of a flat constant. An oversized image drives the whole
    composition over QWEN_VISION_MAX_PROMPT_BUDGET, which
    compose_within_budget()'s existing hard-overflow path already fails
    closed on -- zero inference, exactly like every other HARD
    contribution, with no new logic required here."""
    contribution = Contribution(kind or RESOURCE_ENCOUNTER, "[one photograph admitted to this request]", hard=True)
    contribution.cost = qwen_image_admission_cost(width, height)
    return contribution


def calibrated_or_fallback_cost(rendered_text, live_fingerprint, expected_fingerprint, calibrated_cost):
    """OWC9-P3B: the ONLY sanctioned narrow override of estimate_tokens()'s
    own authority as a Contribution's cost -- restricted BY CONVENTION
    (not a generic API any caller may exploit) to the one fingerprinted,
    empirically-calibrated Pass-1 immutable scaffold this gate measured
    against the real local model via a bounded, explicitly-authorized
    calibration assay (see llama_anaxi.py's own calibration record and
    this gate's final report for the full methodology: 5 stable deltas,
    all exactly 816 tokens, variance 0; 8/8 full-prompt coverage checks
    satisfied; calibrated_cost = max(delta) + 8 = 824).

    This is deliberately NOT a general 'caller supplies any cost it
    likes' escape hatch: it requires the caller's OWN live_fingerprint
    dict to match expected_fingerprint EXACTLY -- every key, every
    value -- before ever returning anything other than the ordinary,
    always-safe byte-count estimate. A different scaffold text, a
    different model digest, a different schema, or ANY other drift
    falls back to estimate_tokens(rendered_text) unconditionally,
    identically to every other Contribution in this module. No
    automatic recalibration happens here or anywhere in this function --
    a mismatch simply means the calibrated fast path is unavailable
    this turn; the conservative byte estimator remains fully authoritative
    and safe on its own, exactly as it always has been."""
    if live_fingerprint == expected_fingerprint:
        return calibrated_cost
    return estimate_tokens(rendered_text)


# ------------------------------------------------- contribution kinds -----

# spec section 7: the smallest abstraction that makes composition
# explicit and testable. RESOURCE_ENCOUNTER and SLEEP_DERIVED_CONTEXT
# are future-ready enum members only -- no production code produces a
# contribution of either kind yet (spec section 20: no capability
# implementation in this gate). LIVE_WAKING_CONTINUITY is likewise
# named for future-readiness/parity with workspace_roaming.py's own
# analogous (and, per this gate's own report, still-unprotected)
# Stage-1 prompt -- it is not wired to any waking-side contribution
# source in this gate, since LIVE_WAKING_CONTINUITY_V1 is rendered into
# roaming's own prompt (build_roaming_choice_messages()), never into
# run_waking_turn()'s ordinary Pass-1/Pass-2 calls.
CORE_SYSTEM_CONTROL = "core_system_control"
CURRENT_HUMAN_MESSAGE = "current_human_message"
MECHANICAL_STATE = "mechanical_state"
WORKING_SET = "working_set"
RECENT_DIALOGUE = "recent_dialogue"
LIVE_WAKING_CONTINUITY = "live_waking_continuity"          # OWC9-P1: now wired -- roaming Stage-1 only
ACTIVE_WORKSPACE_CONTINUITY = "active_workspace_continuity"
EPISODE_CONTEXT = "episode_context"
RETRIEVED_HISTORY = "retrieved_history"                     # OWC9-P2: now wired -- hippocampal retrieval, ordinary waking Pass-2 only
RESOURCE_ENCOUNTER = "resource_encounter"                   # future-ready, inert this gate
SLEEP_DERIVED_CONTEXT = "sleep_derived_context"              # future-ready, inert this gate
# BOUNDARY INSPECTOR v1: host-mechanical boundary-inspection report,
# offered as a SOFT, Pass-2-only contribution on a later lawful waking
# turn (never Pass 1). Framed as a host report, never Clark's own
# conclusion; competes under Pass-2's ordinary aggregate budget and may
# be dropped by budget like any other SOFT source -- a budget-dropped
# result is simply NOT durably acknowledged (no delivered marker).
BOUNDARY_INSPECTION_RESULT = "boundary_inspection_result"

# OD1 (Clark-Controlled Reversible Operative Directive V0): Clark's own
# active standing directive, offered in the existing pinned-SOFT lane
# (Contribution.minimum_units=1) as Pass-2-only
# contribution (never Pass 1 -- Pass 1 decides only the typed act, and
# never needs to see the directive's own text to do that). SOFT
# because the directive is explicitly LOWER PRECEDENCE than the HARD
# host/system contributions (CORE_SYSTEM_CONTROL, CURRENT_HUMAN_
# MESSAGE, MECHANICAL_STATE, WORKING_SET) -- a directive competing for
# HARD status would contradict that precedence. Pinned SOFT means all
# ordinary droppable context is removed first and composition fails
# truthfully if HARD controls plus the directive cannot fit; active
# state is never silently reported while carriage is omitted. Absent entirely (no
# Contribution constructed at all) when no directive is active, so the
# null state adds nothing to the prompt (O11).
OPERATIVE_DIRECTIVE = "operative_directive"

# READ-ONLY EXTERNAL INFORMATION CAPABILITY V0: a host-mechanical
# search/fetch result, offered as a SOFT, Pass-2-only contribution on a
# later lawful waking turn (never Pass 1) -- exactly mirroring
# BOUNDARY_INSPECTION_RESULT's own placement/reasoning immediately
# above. Framed as a host report of retrieved external material, never
# Clark's own knowledge or conclusion; competes under Pass-2's ordinary
# aggregate budget and may be dropped by budget like any other SOFT
# source -- a budget-dropped result is simply NOT durably acknowledged
# (no delivered marker, no carriage component).
EXTERNAL_INFO_RESULT = "external_info_result"

# PRIVATE DISCORD CORRESPONDENCE V0: one pending received Discord
# message, offered as a SOFT, Pass-2-only contribution (never Pass 1) --
# exactly mirroring EXTERNAL_INFO_RESULT's own placement/reasoning. The
# text is host-framed, explicitly-labelled untrusted external data, never
# an instruction and never Clark's own knowledge; it competes under
# Pass-2's ordinary aggregate budget and may be dropped by budget like any
# other SOFT source -- a budget-dropped message is simply NOT durably
# acknowledged (no delivered marker, no carriage component).
DISCORD_INBOUND_CARRIAGE = "discord_inbound_carriage"

# OWC9-P1: roaming Stage-1's own contributor kinds (spec section 3).
# Smallest source-conventional additions -- one per distinct existing
# roaming render source, no speculative kinds beyond actual need.
RECENT_PUBLIC_ROAMING = "recent_public_roaming"       # Bridge B: render_recent_public_window()
ROAMING_HANDOFF = "roaming_handoff"                   # Bridge A: render_departure_handoff()
LAST_WORKSPACE_OBSERVATION = "last_workspace_observation"  # render_previous_observation()

# OWC9-P2 (spec section 7): legacy waking retrieval gets its own tiny
# kind, distinct from RETRIEVED_HISTORY (now activated for hippocampal
# retrieval) -- materially useful because the two sources have
# genuinely different unit shapes (legacy: one atomic quarantined
# block, per source audit section 5's narrow-repair allowance;
# hippocampal: independently droppable, already-structured, source-
# grounded RetrievedMemory items) and compose_within_budget() keeps at
# most one Contribution per kind (soft_by_kind is a dict keyed by
# kind) -- they cannot share RETRIEVED_HISTORY without one silently
# overwriting the other.
LEGACY_RETRIEVAL = "legacy_retrieval"

# Waking-pass2-budget repair: Pass-2's own fixed, non-Kardia framing
# text (CONVERSATION_MODE_CLAUSE + the aesthetic directive) needed its
# own distinct kind, separate from CORE_SYSTEM_CONTROL (which now holds
# only the genuinely dynamic, evolving Kardia-derived identity text) --
# same "compose_within_budget() keeps at most one Contribution per
# kind" reasoning as LEGACY_RETRIEVAL's own comment above; the two
# pieces have genuinely different accounting treatment (one calibrated
# and permanently fixed, one always byte-estimated and evolving) and
# would silently overwrite each other under one shared kind.
CONVERSATION_FRAMING = "conversation_framing"

# The fixed (turn-invariant) block of the Pass-1 action menu -- the per-capability
# field lines and examples. Its own kind for the same reason CONVERSATION_FRAMING
# is: compose_within_budget() keeps one Contribution per kind, and this block has a
# different accounting treatment (fingerprint-calibrated) from the turn-dependent
# mechanical state it rides next to (byte-estimated).
ACTION_MENU_FIXED = "pass1_action_menu"

# Pass-1-only: the search result choices a fetch_url "result:N" may select. Its own
# kind (one Contribution per kind) so it can be protected in Pass 1 -- where it competes
# with recent dialogue -- without changing where EXTERNAL_INFO_RESULT (Pass 2) trims.
# Live (2026-09-20): sharing EXTERNAL_INFO_RESULT's early trim slot, it was the first thing
# dropped from a real Pass 1 and the subject was left to invent a fetch target.
PASS1_SEARCH_CHOICES = "pass1_search_choices"

# Pass-2-only: the source identity (title, final URL, status, completeness) of a page fetched on an
# earlier turn. Live (2026-09-20): page text rides only on the fetching turn, so a later "which
# source was that?" had no URL in front of the subject. Small, SOFT, trimmed late.
SOURCE_PROVENANCE = "source_provenance"

# Pass-1-only: the last web page Clark opened (URL, which part reached him, exact reopen/read-on
# targets), as a data message beside his current turn like PASS1_SEARCH_CHOICES. Live (2026-09-24):
# stated only in the system menu, the URL did not bind -- in the live conversation the subject never
# reopened it, and with the request made he wrote placeholder URLs; beside the turn, 18/18.
PASS1_OPEN_PAGE = "pass1_open_page"

ALL_CONTRIBUTION_KINDS = frozenset({
    CORE_SYSTEM_CONTROL, CURRENT_HUMAN_MESSAGE, MECHANICAL_STATE, WORKING_SET,
    RECENT_DIALOGUE, LIVE_WAKING_CONTINUITY, ACTIVE_WORKSPACE_CONTINUITY,
    EPISODE_CONTEXT, RETRIEVED_HISTORY, RESOURCE_ENCOUNTER, SLEEP_DERIVED_CONTEXT,
    RECENT_PUBLIC_ROAMING, ROAMING_HANDOFF, LAST_WORKSPACE_OBSERVATION,
    LEGACY_RETRIEVAL, CONVERSATION_FRAMING, ACTION_MENU_FIXED,
    BOUNDARY_INSPECTION_RESULT, OPERATIVE_DIRECTIVE, EXTERNAL_INFO_RESULT,
    DISCORD_INBOUND_CARRIAGE, PASS1_SEARCH_CHOICES, SOURCE_PROVENANCE, PASS1_OPEN_PAGE,
})

# spec section 9: fixed, deterministic, non-semantic trim order --
# SOFT contributions only, earliest-dropped first. HARD contributions
# never appear here; an oversized HARD set fails closed instead (spec
# sections 6/13/14) rather than being trimmed. RETRIEVED_HISTORY/
# RESOURCE_ENCOUNTER/SLEEP_DERIVED_CONTEXT are listed for future-
# readiness (their eventual position in the order, per spec section 9's
# own preferred policy) even though no production call site produces
# one yet this gate -- an empty/absent contribution of any of these
# kinds is simply never seen by the trim loop.
#
# SINGLE shared order across BOTH waking and roaming compositions
# (OWC9-P1 spec section 3: "reuse OWC9 central authority, do not fork
# it") -- waking and roaming never contribute overlapping kinds in the
# same compose_within_budget() call (roaming's RECENT_PUBLIC_ROAMING/
# ROAMING_HANDOFF/LAST_WORKSPACE_OBSERVATION never appear alongside
# waking's EPISODE_CONTEXT/ACTIVE_WORKSPACE_CONTINUITY, and vice
# versa), so one fixed tuple correctly encodes both pathways' own
# independent priority without conflict. Roaming's own priority (spec
# section 11: LAST_WORKSPACE_OBSERVATION is "the one genuine content
# encounter" and must be the LAST thing dropped): RECENT_PUBLIC_ROAMING
# and ROAMING_HANDOFF are dropped before LIVE_WAKING_CONTINUITY
# (reusing its existing waking-side position), and
# LAST_WORKSPACE_OBSERVATION is placed absolute-last -- the most
# protected kind of all, in either pathway.
#
# OWC9-P2 (spec section 7): "existing SOFT_TRIM_ORDER already places
# retrieval first [dropped first] -- preserve that principle." LEGACY_
# RETRIEVAL is placed even earlier than RETRIEVED_HISTORY: legacy
# memory's own quarantine header self-describes it as LEGACY/UNTYPED/
# NON-AUTHORITATIVE (render_legacy_memory_quarantine()), while
# hippocampal retrieval is source-grounded/attributed -- the strictly
# less-authoritative source is dropped strictly first.
SOFT_TRIM_ORDER = (
    LEGACY_RETRIEVAL,
    RETRIEVED_HISTORY,
    RESOURCE_ENCOUNTER,
    # BOUNDARY INSPECTOR v1: placed right alongside RESOURCE_ENCOUNTER
    # (the other host-authored mechanical-fact class) -- SOFT, earliest-
    # dropped-first semantics are unchanged; a budget-tight Pass-2 may
    # drop a boundary report with no durable consequence.
    BOUNDARY_INSPECTION_RESULT,
    # READ-ONLY EXTERNAL INFORMATION CAPABILITY V0: placed immediately
    # alongside BOUNDARY_INSPECTION_RESULT/RESOURCE_ENCOUNTER (the other
    # host-authored, host-mechanical-fact classes) -- SOFT, earliest-
    # dropped-first semantics are unchanged; a budget-tight Pass-2 may
    # drop a search/fetch result with no durable consequence (it simply
    # remains pending for a later turn's Pass-2 pick-up).
    EXTERNAL_INFO_RESULT,
    # PRIVATE DISCORD CORRESPONDENCE V0: placed immediately alongside
    # EXTERNAL_INFO_RESULT (the other host-authored, host-mechanical
    # external-material class, itself earliest-dropped-first) -- SOFT, so
    # a budget-tight Pass-2 may drop a received message with no durable
    # consequence (it simply remains pending for a later turn).
    DISCORD_INBOUND_CARRIAGE,
    EPISODE_CONTEXT,
    ACTIVE_WORKSPACE_CONTINUITY,
    RECENT_PUBLIC_ROAMING,
    ROAMING_HANDOFF,
    LIVE_WAKING_CONTINUITY,
    RECENT_DIALOGUE,
    # Trimmed only after older dialogue units have gone (a small, bounded choice list is
    # worth more to the current decision than the oldest turns of history).
    PASS1_SEARCH_CHOICES,
    PASS1_OPEN_PAGE,
    SOURCE_PROVENANCE,
    # OD1: placed deliberately late in trim order -- a small (bounded,
    # see operative_directive.MAX_OPERATIVE_DIRECTIVE_TEXT_LENGTH),
    # deliberately Clark-adopted standing choice should survive all but
    # the tightest budget pressure, unlike incidental retrieved/roaming
    # content. Still strictly SOFT and always lower precedence than HARD
    # host/system mechanics; its one pinned unit cannot be silently
    # dropped, so an impossible HARD+directive sum fails composition.
    OPERATIVE_DIRECTIVE,
    LAST_WORKSPACE_OBSERVATION,
)


class Contribution:
    """One named, costed, typed unit of prompt content (spec section 7).

    `hard=True` contributions are NEVER trimmed or dropped -- if the
    hard set alone cannot fit under budget, composition fails closed
    (BUDGET_EXCEEDED) rather than silently shrinking one (spec sections
    6/13: current human message and core control state must not
    disappear silently).

    `hard=False` (soft) contributions are trimmed per SOFT_TRIM_ORDER.
    A soft contribution is either:
      - a single atomic block (`droppable_units is None`): trimmed
        all-or-nothing (this is the correct, non-tearing behavior for
        an already-bounded rendered text block produced by an existing
        subsystem renderer -- e.g. Bridge C's episode-context text,
        ACTIVE_WORKSPACE_CONTINUITY_V1's rendered text -- re-slicing
        inside it would tear structured/labeled content, violating
        governing invariant G); or
      - a list of already-ordered, already-complete units
        (`droppable_units` given, oldest-first, e.g. OWC8's own
        dialogue-window pairs): trimmed unit-by-unit from the oldest
        end, preserving newest-first, never tearing a single unit,
        exactly mirroring each family's own existing recency policy
        (spec section 9's "within a contribution family... preserve
        newest complete units").

    `render_fn(units) -> str` re-renders the family's own existing
    format from a (possibly reduced) unit list -- never a new format,
    never semantic re-summarization (spec section 9: "do not silently
    rewrite semantic content")."""

    # minimum_units pins a newest suffix inside an otherwise soft family.
    # If hard controls plus that suffix cannot fit, composition fails closed.
    # Default zero preserves all existing callers' trimming behavior.
    def __init__(self, kind, rendered_text, hard, droppable_units=None,
                 render_fn=None, source_ids=None, minimum_units=0):
        if kind not in ALL_CONTRIBUTION_KINDS:
            raise ValueError(f"unknown contribution kind: {kind!r}")
        if droppable_units is not None and render_fn is None:
            raise ValueError("droppable_units requires render_fn")
        if not isinstance(minimum_units, int) or minimum_units < 0 or minimum_units > len(droppable_units or []):
            raise ValueError("minimum_units must name an existing retained suffix")
        self.kind = kind
        self.rendered_text = rendered_text or ""
        self.hard = hard
        self.droppable_units = list(droppable_units) if droppable_units is not None else None
        self.render_fn = render_fn
        self.source_ids = list(source_ids) if source_ids is not None else None
        self.minimum_units = minimum_units
        self.cost = estimate_tokens(self.rendered_text)

    def is_empty(self):
        return not self.rendered_text


class CompositionResult:
    """The compositor's full, testable output (spec section 15's own
    diagnostic-field list maps directly onto these attributes)."""

    def __init__(self):
        self.included = []          # Contribution objects, in original order, surviving (possibly reduced)
        self.dropped_kinds = []     # kinds dropped ENTIRELY (soft, all-or-nothing or fully emptied)
        self.trimmed_kinds = []     # kinds partially reduced (units dropped, some remain)
        self.final_prompt_cost = 0
        self.max_prompt_budget = 0
        self.fits = False

    def included_kind(self, kind):
        for c in self.included:
            if c.kind == kind:
                return c
        return None

    def delivered_source_ids(self, kind):
        """The exact source_ids that actually survived into the final
        composed prompt for `kind` -- spec section 18's own required
        distinction ("NOT SELECTED THIS TURN DUE TO BUDGET != DURABLY
        DELIVERED"). Returns [] if the kind was dropped entirely or
        never contributed source_ids at all."""
        c = self.included_kind(kind)
        if c is None or c.source_ids is None:
            return []
        return list(c.source_ids)


def _cost_of(contribution, rendered_text):
    """Cost of a (re-)rendered contribution: its measured ``cost_fn`` when one
    was attached by recost_by_measurement(), otherwise the byte upper bound."""
    cost_fn = getattr(contribution, "cost_fn", None)
    return cost_fn(rendered_text) if cost_fn is not None else estimate_tokens(rendered_text)


def compose_within_budget(contributions, max_prompt_budget, *, refill_unused_capacity=False):
    """Pure, deterministic (spec section D/E/F: no model call, no
    salience heuristic -- purely mechanical arithmetic and fixed
    ordering). Returns a CompositionResult.

    Algorithm:
      1. Sum HARD contribution costs. If that alone exceeds budget,
         fail closed immediately (spec section 6/13) -- `fits=False`,
         `included` still lists the hard set for diagnostic purposes,
         but the caller must not proceed to a model call.
      2. Add all SOFT contributions; if the total already fits, done --
         nothing trimmed or dropped.
      3. Otherwise walk SOFT_TRIM_ORDER once. For each soft kind
         present: if it has droppable_units, drop the OLDEST remaining
         unit and re-render/re-cost, repeating until either the total
         fits or the family is fully empty (then drop it as a whole
         entry); if it is atomic, drop it whole. Recompute the running
         total after every single drop (spec section D: mechanical,
         one deterministic step at a time, never a batch guess).
      4. Stop as soon as the total fits. Kinds never reached by the
         walk are untouched. A kind absent from `contributions` is
         simply not touched (nothing to trim).
      5. When refill_unused_capacity is enabled by ordinary waking, try
         restoring dropped units in reverse trim order using only leftover
         capacity. Existing survivors and their priority remain unchanged.
         Source IDs follow the same unit slices as rendered content."""
    # Ordinary waking can reclaim slack left by indivisible dialogue drops.
    # Preserve the original units before the existing trim pass mutates them.
    import copy
    originals = {}
    if refill_unused_capacity:
        for c in contributions:
            if not c.hard:
                original = copy.copy(c)
                original.droppable_units = list(c.droppable_units) if c.droppable_units is not None else None
                original.source_ids = list(c.source_ids) if c.source_ids is not None else None
                originals[c.kind] = original
    hard = [c for c in contributions if c.hard]
    soft = [c for c in contributions if not c.hard]
    soft_by_kind = {c.kind: c for c in soft}

    result = CompositionResult()
    result.max_prompt_budget = max_prompt_budget

    hard_cost = sum(c.cost for c in hard)
    if hard_cost > max_prompt_budget:
        result.included = list(hard)
        result.final_prompt_cost = hard_cost
        result.fits = False
        return result

    def total_cost():
        return hard_cost + sum(c.cost for c in soft if not soft_dropped.get(c.kind))

    soft_dropped = {}
    if hard_cost + sum(c.cost for c in soft) <= max_prompt_budget:
        result.included = list(hard) + list(soft)
        result.final_prompt_cost = hard_cost + sum(c.cost for c in soft)
        result.fits = True
        return result

    for kind in SOFT_TRIM_ORDER:
        c = soft_by_kind.get(kind)
        if c is None or c.is_empty():
            continue
        if c.droppable_units is not None:
            trimmed_any = False
            while len(c.droppable_units) > c.minimum_units and total_cost() > max_prompt_budget:
                c.droppable_units.pop(0)  # oldest first -- newest preserved
                if c.source_ids is not None:
                    c.source_ids = c.source_ids[1:]
                c.rendered_text = c.render_fn(c.droppable_units)
                c.cost = _cost_of(c, c.rendered_text)
                trimmed_any = True
            if not c.droppable_units:
                soft_dropped[kind] = True
                if kind not in result.dropped_kinds:
                    result.dropped_kinds.append(kind)
            elif trimmed_any:
                result.trimmed_kinds.append(kind)
        else:
            if total_cost() > max_prompt_budget:
                soft_dropped[kind] = True
                result.dropped_kinds.append(kind)
        if total_cost() <= max_prompt_budget:
            break

    if refill_unused_capacity:
        # Highest existing priority first; never displace a survivor, change
        # the ceiling, summarize content, or skip a newer unit for an older one.
        for kind in reversed(SOFT_TRIM_ORDER):
            original = originals.get(kind)
            if original is None or original.is_empty():
                continue
            c = soft_by_kind[kind]
            current_cost = 0 if soft_dropped.get(kind) else c.cost
            if original.droppable_units is None:
                candidates = [(original.rendered_text, original.cost, None, original.source_ids)]
            else:
                retained = 0 if soft_dropped.get(kind) else len(c.droppable_units)
                candidates = []
                for n in range(retained + 1, len(original.droppable_units) + 1):
                    units = original.droppable_units[-n:]
                    rendered = original.render_fn(units)
                    ids = original.source_ids[-n:] if original.source_ids is not None else None
                    candidates.append((rendered, _cost_of(c, rendered), units, ids))
            for rendered, cost, units, ids in candidates:
                if total_cost() - current_cost + cost > max_prompt_budget:
                    break
                c.rendered_text, c.cost, c.droppable_units, c.source_ids = rendered, cost, units, ids
                soft_dropped.pop(kind, None)
                current_cost = cost
            if not soft_dropped.get(kind):
                if kind in result.dropped_kinds:
                    result.dropped_kinds.remove(kind)
                if c.droppable_units == original.droppable_units:
                    if kind in result.trimmed_kinds:
                        result.trimmed_kinds.remove(kind)
                elif kind not in result.trimmed_kinds:
                    result.trimmed_kinds.append(kind)
    surviving_soft = [c for c in soft if not soft_dropped.get(c.kind) and not c.is_empty()]
    result.included = list(hard) + surviving_soft
    result.final_prompt_cost = total_cost()
    result.fits = result.final_prompt_cost <= max_prompt_budget
    return result


def _snapshot(contributions):
    """Independent copies, as compose_within_budget() itself makes when it
    preserves originals: dry runs must never mutate a live contribution."""
    import copy
    copies = []
    for c in contributions:
        clone = copy.copy(c)
        clone.droppable_units = list(c.droppable_units) if c.droppable_units is not None else None
        clone.source_ids = list(c.source_ids) if c.source_ids is not None else None
        copies.append(clone)
    return copies


def would_shed(contributions, max_prompt_budget, *, refill_unused_capacity=False):
    """True when composing these contributions would fail, drop or trim any
    soft material. Pure dry run on copies; the inputs are untouched."""
    result = compose_within_budget(
        _snapshot(contributions), max_prompt_budget, refill_unused_capacity=refill_unused_capacity,
    )
    return (not result.fits) or bool(result.dropped_kinds) or bool(result.trimmed_kinds)


MEASURED_BOUNDARY_TOKENS = 8   # per-message chat-template boundary slack, as every calibration here uses


def recost_by_measurement(contributions, measure_fn, *, skip_kinds=()):
    """Replace the byte upper-bound cost of each non-empty ATOMIC contribution
    with its real provider-measured cost (plus boundary slack), never above
    the estimator's bound.

    The estimator over-prices prose ~4x, so with production-sized identity and
    control text the byte-costed hard floor leaves almost no room and every
    soft kind (dialogue, retrieval, results) is shed on ordinary turns. Real
    measurement is exact for the provider's own tokenizer; ``measure_fn(text)
    -> int | None`` (None = not safely measurable: the estimator cost stays).
    Unit-based contributions keep the estimator (they re-cost per drop).
    Returns the number of contributions whose cost was lowered."""
    lowered = 0
    for c in contributions:
        # A single-unit family (e.g. the pinned operative directive) is one
        # indivisible block for costing; multi-unit families re-cost per drop.
        if c.kind in skip_kinds or c.is_empty():
            continue
        if c.droppable_units is not None and len(c.droppable_units) != 1:
            # A multi-unit family re-costs every subset it renders (per drop, per
            # refill). Attach the measured cost function so each candidate
            # rendering is priced by the provider, never above the byte bound.
            if c.render_fn is not None and c.cost == estimate_tokens(c.rendered_text):
                def _measured(text, _fn=measure_fn):
                    real = _fn(text)
                    bound = estimate_tokens(text)
                    if type(real) is int and real > 0:
                        return min(bound, real + MEASURED_BOUNDARY_TOKENS)
                    return bound
                c.cost_fn = _measured
                new_cost = c.cost_fn(c.rendered_text)
                if new_cost < c.cost:
                    c.cost = new_cost
                    lowered += 1
            continue
        if c.cost != estimate_tokens(c.rendered_text):
            # A cost set deliberately: calibrated (lower), measured (lower), or
            # a modality admission cost such as an image's (HIGHER than its
            # placeholder text, which must never be re-priced by that text).
            continue
        measured = measure_fn(c.rendered_text)
        if type(measured) is not int or measured <= 0:
            continue
        candidate = measured + MEASURED_BOUNDARY_TOKENS
        if candidate < c.cost:
            c.cost = candidate
            lowered += 1
    return lowered


def admit_by_measurement(result, messages, max_prompt_budget, measure_fn):
    """Second-chance admission for a prompt the byte upper bound rejected.

    The estimator over-prices prose ~4x, so a lawful whole unit (one exchange)
    can be rejected as 'oversize' when its real cost fits comfortably. Given a
    failed ``result`` and a ``measure_fn(messages) -> int | None`` that returns
    the provider's own token count for the EXACT prompt, admit iff that real
    count is within budget. ``None`` measurer / failed probe / a count that
    does not fit leave the rejection intact (true overflow stays a failure);
    a prompt whose byte bound is hopelessly beyond budget is never probed."""
    if result.fits or measure_fn is None:
        return result
    if result.final_prompt_cost > 6 * max_prompt_budget:
        return result
    measured = measure_fn(messages)
    if type(measured) is int and 0 < measured <= max_prompt_budget:
        result.fits = True
        result.final_prompt_cost = measured
        result.measured = True
    return result


class BudgetExceededError(Exception):
    """Raised by callers (never by compose_within_budget itself, which
    always returns a result even on failure) when a HARD-only set
    already exceeds budget -- the host-side containment signal for
    'do not call the model with a doomed prompt' (spec section 14).
    Deliberately distinct from ConversationDirectionFailure/MALFORMED_
    ACT -- a host-side budget failure must never be misreported as a
    model-output shape failure (spec section 14: 'Do not misuse
    MALFORMED_ACT for a host-side budget failure')."""

    def __init__(self, result):
        self.result = result
        super().__init__(
            f"aggregate prompt budget exceeded by hard contributions alone: "
            f"{result.final_prompt_cost} > {result.max_prompt_budget}"
        )
