"""Exact local prompt-token counts for the pinned Ministral 3 14B waking substrate.

``llama_anaxi._measure_real_text_cost`` prices a text by two live ``num_predict=1``
calls (with and without the text) and subtracts Ollama's ``prompt_eval_count``.
Nothing is generated; the calls exist only to learn a token count. On the adopted
Ministral substrate, Ollama 0.34.0 serves the weights through its bundled llama.cpp
``llama-server``, which tokenizes the rendered prompt with the GGUF's own vocabulary
and merges. This module reproduces that count exactly for the one pinned substrate and
returns None (the caller keeps the live probe) whenever that identity is not established.

Identity binding, all required, any mismatch -> None:
  * waking tag, manifest digest, chat-template sha256 and Ollama server version equal
    the qualified values (read from the same once-per-process environment check the
    calibrations use);
  * the manifest file read here hashes to the pinned digest, and the tokenizer metadata
    read from its model layer hashes to ``_TOKENIZER_SHA256``;
  * Python's Unicode database is the qualified version;
  * the request is [system], [system, user] (the shapes ``_measure_real_text_cost`` sends), or a strictly
    alternating [system, ..., user] dialogue measurement (``_measure_retained_dialogue_cost``), with no extra
    keys and no lone surrogates.

Qualification (2026-09-25, Ollama 0.34.0; see MINISTRAL_LOCAL_TOKEN_COST.md): token ids
equal ``llama-server /tokenize`` for every Unicode codepoint in several contexts, and the
with-minus-without delta equals the live probe on the recorded corpus
(``completion_evidence/ministral_token_cost_parity.json``).

The GGUF names its pre-tokenizer "default", yet this llama.cpp build applies the Tekken
expression to it (established against the oracle, not inferred), which is one reason the
local path is bound to the exact Ollama server version.

The algorithm mirrors llama.cpp's tokenizer as this build runs it: special-token
partition (every CONTROL token, parsed in user text too), the Tekken pre-tokenizer
regex evaluated over llama.cpp's category-collapsed text, whole-word vocabulary
lookup before merges, rank-ordered byte-level BPE, and BOS.
"""
import hashlib
import heapq
import json
import os
import re
import struct
import unicodedata

QUALIFIED_SUBSTRATE = {
    "model_tag": "ministral-3:14b-instruct-2512-q4_K_M",
    "model_digest": "4760c35aeb9d9e9c6174c2492562c0b999e80a222804fd96b1915ab72bbcdcf7",
    "chat_template_sha256": "aa65b5aebf89e7dfb3aa05af9220299a7e1cebe3ab97daae3c2e259acbdf278c",
    "ollama_server_version": "0.34.0",
}
_MANIFEST_RELPATH = ("manifests", "registry.ollama.ai", "library", "ministral-3", "14b-instruct-2512-q4_K_M")
_MODEL_LAYER_MEDIA_TYPE = "application/vnd.ollama.image.model"
_TOKENIZER_SHA256 = "d53f171dbc84e7106e4236c7a22b346a5278e8ba19221b6fb348aa446540a5bf"
_UNIDATA_VERSION = "16.0.0"
# The whole local counting contract a cached count depends on (substrate pins, tokenizer, Unicode tables,
# reported offset): part of every memo key that may hold a locally counted value.
TOKENIZER_IDENTITY = "|".join([QUALIFIED_SUBSTRATE["model_tag"], QUALIFIED_SUBSTRATE["model_digest"],
                               QUALIFIED_SUBSTRATE["chat_template_sha256"], QUALIFIED_SUBSTRATE["ollama_server_version"],
                               _TOKENIZER_SHA256, "unicode-" + _UNIDATA_VERSION, "offset-1"])

# Ollama reports prompt_eval_count as llama-server's prompt token count plus one, on every
# qualified prompt (with and without the text alike), so the subtracted delta is unaffected.
_OLLAMA_REPORTED_OFFSET = 1

# llama.cpp's codepoint table in this build is Unicode 15.1. Python's 16.0 database adds
# these letters and digits, which llama.cpp still classifies as neither letter nor number.
# Extracted from the running llama-server's own table: the only class differences.
_UNICODE_16_ADDITIONS = (
    (0x1C89, 0x1C8A), (0xA7CB, 0xA7CD), (0xA7DA, 0xA7DC), (0x105C0, 0x105F3), (0x10D40, 0x10D49),
    (0x10D4A, 0x10D65), (0x10D6F, 0x10D85), (0x10EC2, 0x10EC4), (0x11380, 0x11389), (0x1138B, 0x1138B),
    (0x1138E, 0x1138E), (0x11390, 0x113B5), (0x113B7, 0x113B7), (0x113D1, 0x113D1), (0x113D3, 0x113D3),
    (0x116D0, 0x116E3), (0x11BC0, 0x11BE0), (0x11BF0, 0x11BF9), (0x13460, 0x143FA), (0x16100, 0x1611D),
    (0x16130, 0x16139), (0x16D40, 0x16D6C), (0x16D70, 0x16D79), (0x18CFF, 0x18CFF), (0x1CCF0, 0x1CCF9),
    (0x1E5D0, 0x1E5ED), (0x1E5F0, 0x1E5F0), (0x1E5F1, 0x1E5FA),
)
_NEITHER_LETTER_NOR_NUMBER = frozenset(cp for lo, hi in _UNICODE_16_ADDITIONS for cp in range(lo, hi + 1))
# Unicode White_Space, as llama.cpp's own whitespace set.
_WHITESPACE = frozenset((*range(0x09, 0x0E), 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B),
                         0x2028, 0x2029, 0x202F, 0x205F, 0x3000))
# llama.cpp collapses each non-ASCII codepoint to one byte per category before regex matching.
# The Tekken expression tests only \p{L} and \p{N}, so every other category collapses alike.
_COLLAPSED = {"N": 0xD1, "L": 0xD2}
_OTHER = 0xD0
_L = rb"\xd2A-Za-z"
_N = rb"\xd10-9"
# llama.cpp's LLAMA_VOCAB_PRE_TYPE_TEKKEN expression, after its \p{..} collapse.
_TEKKEN = re.compile(
    rb"[^\r\n" + _L + _N + rb"]?((?=[" + _L + rb"])([^a-z]))*((?=[" + _L + rb"])([^A-Z]))+"
    rb"|[^\r\n" + _L + _N + rb"]?((?=[" + _L + rb"])([^a-z]))+((?=[" + _L + rb"])([^A-Z]))*"
    rb"|[" + _N + rb"]"
    rb"| ?[^\s" + _L + _N + rb"]+[\r\n/]*"
    rb"|\s*[\r\n]+|\s+(?!\S)|\s+"
)


def _byte_to_unicode():
    printable = [*range(0x21, 0x7F), *range(0xA1, 0xAD), *range(0xAE, 0x100)]
    mapping, extra = {b: chr(b) for b in printable}, 0
    for b in range(256):
        if b not in mapping:
            mapping[b] = chr(256 + extra)
            extra += 1
    return mapping


_BYTE_TO_UNICODE = _byte_to_unicode()


def _collapsed_class(cp):
    if cp < 128:
        return cp
    if cp in _WHITESPACE:
        return 0x0B
    if cp in _NEITHER_LETTER_NOR_NUMBER:
        return _OTHER
    return _COLLAPSED.get(unicodedata.category(chr(cp))[0], _OTHER)


def _read_gguf_metadata(path):
    """The key/value header only; tensor data is never read."""
    scalar = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
    with open(path, "rb") as f:
        def read(fmt):
            return struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]

        def string():
            return f.read(read("<Q")).decode("utf-8")

        def value(kind):
            if kind in scalar:
                return read(scalar[kind])
            if kind == 8:
                return string()
            if kind == 9:
                item_kind, count = read("<I"), read("<Q")
                return [value(item_kind) for _ in range(count)]
            raise ValueError(f"unknown GGUF value type {kind}")

        if f.read(4) != b"GGUF" or read("<I") != 3:
            raise ValueError("not a GGUF v3 file")
        _tensor_count, kv_count = read("<Q"), read("<Q")
        return {string(): value(read("<I")) for _ in range(kv_count)}


def _models_dir():
    return os.environ.get("OLLAMA_MODELS") or os.path.join(os.path.expanduser("~"), ".ollama", "models")


class _Tokenizer:
    def __init__(self, meta):
        tokens, types = meta["tokenizer.ggml.tokens"], meta["tokenizer.ggml.token_type"]
        self.token_ids = {}
        for i, token in enumerate(tokens):
            self.token_ids[token or f"[EMPTY_{i}]"] = i
        self.ranks = {}
        for rank, merge in enumerate(meta["tokenizer.ggml.merges"]):
            cut = merge.find(" ", 1)
            self.ranks.setdefault((merge[:cut], merge[cut + 1:]), rank)
        # llama.cpp parses CONTROL / USER_DEFINED / UNKNOWN token texts as special tokens.
        self.specials = sorted({tokens[i] for i, t in enumerate(types) if t in (2, 3, 4)},
                               key=lambda s: -len(s.encode("utf-8")))
        self.bos = [meta["tokenizer.ggml.bos_token_id"]] if meta["tokenizer.ggml.add_bos_token"] else []
        self._words = {}

    def encode(self, text):
        """llama.cpp token ids for ``text`` with special-token parsing and BOS."""
        fragments = [text]
        for special in self.specials:
            split = []
            for fragment in fragments:
                if isinstance(fragment, int) or special not in fragment:
                    split.append(fragment)
                    continue
                for k, part in enumerate(fragment.split(special)):
                    if k:
                        split.append(self.token_ids[special])
                    if part:
                        split.append(part)
            fragments = split
        ids = list(self.bos)
        for fragment in fragments:
            if isinstance(fragment, int):
                ids.append(fragment)
                continue
            for word in _pretokenize(fragment):
                cached = self._words.get(word)
                if cached is None:
                    cached = self._words[word] = self._bpe(word)
                ids.extend(cached)
        return ids

    def _bpe(self, word):
        symbols = [_BYTE_TO_UNICODE[b] for b in word.encode("utf-8")]
        whole = self.token_ids.get("".join(symbols))
        if whole is not None:
            return [whole]
        n = len(symbols)
        nxt, prv = list(range(1, n)) + [-1], list(range(-1, n - 1))
        queue = []

        def push(left, right):
            rank = self.ranks.get((symbols[left], symbols[right]))
            if rank is not None:
                heapq.heappush(queue, (rank, left, symbols[left] + symbols[right], right))

        for i in range(n - 1):
            push(i, i + 1)
        while queue:
            _rank, left, merged, right = heapq.heappop(queue)
            if not symbols[left] or not symbols[right] or symbols[left] + symbols[right] != merged:
                continue
            symbols[left], symbols[right] = merged, ""
            nxt[left] = nxt[right]
            if nxt[right] != -1:
                prv[nxt[right]] = left
            if prv[left] != -1:
                push(prv[left], left)
            if nxt[left] != -1:
                push(left, nxt[left])
        ids, i = [], 0
        while i != -1:
            token = self.token_ids.get(symbols[i])
            if token is not None:
                ids.append(token)
            else:   # llama.cpp's per-byte fallback keeps only bytes that are tokens themselves
                ids.extend(self.token_ids[chr(b)] for b in symbols[i].encode("utf-8")
                           if b < 0x80 and chr(b) in self.token_ids)
            i = nxt[i]
        return ids


def _pretokenize(text):
    """llama.cpp's regex split over the collapsed text: every match is a word, and so is
    every unmatched gap (one collapsed byte per codepoint, so offsets are shared)."""
    collapsed = bytes(_collapsed_class(ord(c)) for c in text)
    words, position = [], 0
    for m in _TEKKEN.finditer(collapsed):
        if m.start() > position:
            words.append(text[position:m.start()])
        words.append(text[m.start():m.end()])
        position = m.end()
    if position < len(text):
        words.append(text[position:])
    return words


_state = {"loaded": False, "tokenizer": None}


def _tokenizer():
    """Loaded once per process from the pinned manifest's model layer; None when the
    exact tokenizer cannot be established (the caller keeps the live probe)."""
    if _state["loaded"]:
        return _state["tokenizer"]
    _state["loaded"] = True
    try:
        if unicodedata.unidata_version != _UNIDATA_VERSION:
            return None
        models = _models_dir()
        with open(os.path.join(models, *_MANIFEST_RELPATH), "rb") as f:
            manifest = f.read()
        if hashlib.sha256(manifest).hexdigest() != QUALIFIED_SUBSTRATE["model_digest"]:
            return None
        layer = next(l["digest"] for l in json.loads(manifest)["layers"] if l["mediaType"] == _MODEL_LAYER_MEDIA_TYPE)
        meta = _read_gguf_metadata(os.path.join(models, "blobs", layer.replace(":", "-")))
        if tokenizer_fingerprint(meta) != _TOKENIZER_SHA256:
            return None
        _state["tokenizer"] = _Tokenizer(meta)
    except Exception:
        _state["tokenizer"] = None
    return _state["tokenizer"]


def tokenizer_fingerprint(meta):
    keys = ("tokenizer.ggml.model", "tokenizer.ggml.pre", "tokenizer.ggml.tokens", "tokenizer.ggml.token_type",
            "tokenizer.ggml.merges", "tokenizer.ggml.bos_token_id", "tokenizer.ggml.add_bos_token",
            "tokenizer.ggml.add_eos_token")
    payload = json.dumps([meta.get(k) for k in keys], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _render(messages):
    """The pinned chat template, no tools, for exactly the qualified shapes: a system message, then strictly
    alternating user/assistant messages ending with a user message ([system], [system, user], and the
    retained-dialogue measurement shape [system, user, assistant, ..., user] or [system, assistant, user]).
    Anything else is not counted locally: without a system message the template injects a dated default prompt,
    and Ollama merges consecutive same-role messages before templating (a live mismatch, not a template reading).
    An assistant turn that is not last renders as its content followed by </s>; an empty one is not counted."""
    roles = tuple(m.get("role") if isinstance(m, dict) else None for m in messages)
    if not roles or roles[0] != "system" or "system" in roles[1:]:
        return None
    rest = roles[1:]
    if rest and (rest[-1] != "user" or any(r not in ("user", "assistant") for r in rest)
                 or any(a == b for a, b in zip(rest, rest[1:]))):
        return None
    parts = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, str) or set(m) - {"role", "content"}:
            return None
        if m["role"] == "system":
            parts.append(f"[SYSTEM_PROMPT]{content}[/SYSTEM_PROMPT]")
        elif m["role"] == "user":
            parts.append(f"[INST]{content}[/INST]")
        else:
            if not content:
                return None
            parts.append(f"{content}</s>")
    return "".join(parts)


def substrate_is_qualified(model_tag, environment):
    return (model_tag == QUALIFIED_SUBSTRATE["model_tag"]
            and all(environment.get(k) == QUALIFIED_SUBSTRATE[k]
                    for k in ("model_digest", "chat_template_sha256"))
            and environment.get("server_version") == QUALIFIED_SUBSTRATE["ollama_server_version"])


def prompt_eval_count(model_tag, environment, messages):
    """What Ollama would report as ``prompt_eval_count`` for this exact chat request,
    or None when the local count is not established for it."""
    if not substrate_is_qualified(model_tag, environment):
        return None
    rendered = _render(messages)
    if rendered is None:
        return None
    try:
        rendered.encode("utf-8")   # lone surrogates cannot reach the provider unchanged
    except UnicodeEncodeError:
        return None
    tokenizer = _tokenizer()
    if tokenizer is None:
        return None
    return len(tokenizer.encode(rendered)) + _OLLAMA_REPORTED_OFFSET
