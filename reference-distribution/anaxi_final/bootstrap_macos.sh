#!/bin/bash
# ANAXI macOS bootstrap (Windows->macOS migration prep, 2026-09-06).
#
# Safe to run before Clark is ever started. This script:
#   - reports detected macOS/CPU architecture;
#   - checks (never silently installs beyond what's named below)
#     Python, pip, and Ollama presence/version;
#   - creates a clean, dedicated virtualenv and installs the exact
#     dependency manifest captured from the source (Windows) machine;
#   - reports which required Ollama models are missing, by role, without
#     changing any role assignment;
#   - never creates canonical H/X, never runs Sleep, never launches
#     Clark.
#
# Run it from the restored repository root (the same directory this
# file lives in): ./bootstrap_macos.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$SCRIPT_DIR" || exit 1

FAILED=0
note()  { printf '  %s\n' "$1"; }
ok()    { printf '  [ok] %s\n' "$1"; }
warn()  { printf '  [!!] %s\n' "$1"; FAILED=1; }

echo "=== ANAXI macOS bootstrap ==="
echo

echo "--- Platform ---"
UNAME_S="$(uname -s)"
UNAME_M="$(uname -m)"
note "OS: $UNAME_S"
note "CPU architecture: $UNAME_M"
if [ "$UNAME_S" != "Darwin" ]; then
    warn "This script expects Darwin (macOS); detected '$UNAME_S'. Continuing, but not verified for this OS."
fi
if [ "$UNAME_M" = "arm64" ]; then
    note "Apple Silicon detected."
elif [ "$UNAME_M" = "x86_64" ]; then
    note "Intel Mac detected."
else
    note "Unrecognized architecture '$UNAME_M' -- reported as detected, not assumed."
fi
echo

echo "--- Python ---"
PYTHON_BIN=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON_BIN="$candidate"
        break
    fi
done
if [ -z "$PYTHON_BIN" ]; then
    warn "No python3 found on PATH. Install Python (e.g. via python.org or 'brew install python@3.13') and re-run."
else
    PY_VERSION="$("$PYTHON_BIN" --version 2>&1)"
    ok "Found $PYTHON_BIN -> $PY_VERSION"
    note "Source (Windows) machine ran Python 3.14.2. Prefer the closest available 3.x on this Mac;"
    note "do not assume an exact patch-version match is required, but note any behavioral difference"
    note "you observe so it can be captured for a future gate -- this bootstrap does not silently"
    note "upgrade/downgrade anything for you."
fi
echo

echo "--- Virtual environment ---"
VENV_DIR="$SCRIPT_DIR/.venv-macos"
if [ -n "$PYTHON_BIN" ]; then
    if [ ! -d "$VENV_DIR" ]; then
        "$PYTHON_BIN" -m venv "$VENV_DIR" && ok "Created venv at $VENV_DIR" || warn "Failed to create venv at $VENV_DIR"
    else
        ok "Reusing existing venv at $VENV_DIR"
    fi
    if [ -f "$VENV_DIR/bin/activate" ]; then
        # shellcheck disable=SC1090
        source "$VENV_DIR/bin/activate"
        "$PYTHON_BIN" -m pip install --upgrade pip >/dev/null 2>&1
        if [ -f "requirements-macos.txt" ]; then
            note "Installing pinned dependency manifest (requirements-macos.txt)..."
            if pip install -r requirements-macos.txt; then
                ok "Dependencies installed into $VENV_DIR"
                note "Preparing the required local embedding model (explicit setup network step)..."
                if python prepare_embedding_model.py; then
                    ok "Embedding model is cached and dimension-verified for offline ordinary waking."
                else
                    warn "Required embedding model could not be prepared. Ordinary waking will fail closed rather than download it implicitly."
                fi
            else
                warn "One or more dependencies failed to install -- see pip output above."
                note "Native/compiled packages (torch, faiss-cpu, scipy, safetensors, sentence-transformers)"
                note "may need a Mac-specific wheel or 'brew install' prerequisite; do not casually upgrade"
                note "versions to work around this without checking with the owner first."
            fi
        else
            warn "requirements-macos.txt not found next to this script -- cannot install a known-working dependency set."
        fi
    fi
else
    warn "Skipping venv creation -- no python3 available."
fi
echo

echo "--- Ollama ---"
if command -v ollama >/dev/null 2>&1; then
    OLLAMA_VERSION="$(ollama --version 2>&1)"
    ok "Found ollama -> $OLLAMA_VERSION"
    note "Source machine ran Ollama server version 0.32.15. A different version on this Mac is not"
    note "automatically wrong, but if a model behaves differently, check this first."
else
    warn "ollama not found on PATH. Install it (https://ollama.com/download) and re-run."
fi
echo

echo "--- Required production models (role -> exact identifier) ---"
REQUIRED_MODEL_ROLES=(waking_production sleep vision)
REQUIRED_MODEL_NAMES=("gemma4:e4b" "llama3.2:3b" "qwen3-vl:4b")
if command -v ollama >/dev/null 2>&1; then
    INSTALLED="$(ollama list 2>/dev/null)"
    i=0
    while [ "$i" -lt "${#REQUIRED_MODEL_ROLES[@]}" ]; do
        role="${REQUIRED_MODEL_ROLES[$i]}"
        model="${REQUIRED_MODEL_NAMES[$i]}"
        if echo "$INSTALLED" | grep -q "^${model}[[:space:]]"; then
            ok "$role: $model already present"
        else
            warn "$role: $model NOT installed. Run: ollama pull $model"
        fi
        i=$((i + 1))
    done
    note "Comparison-only models (qwen3:4b-instruct-2507-q4_K_M, ministral-3:3b) are NOT required for"
    note "production waking and are not pulled by this script. Role assignments above are never changed"
    note "by this script regardless of what is or isn't installed."
else
    note "(skipped -- ollama not found)"
fi
echo

echo "--- Paths ---"
note "This bootstrap does not assume the Windows working path"
note "(C:\\path\\to\\anaxi_project\\anaxi_final). It operates entirely relative to"
note "its own location: $SCRIPT_DIR"
if [[ "$SCRIPT_DIR" == *"/Library/CloudStorage/"* ]] || [[ "$SCRIPT_DIR" == *"iCloud"* ]] || [[ "$SCRIPT_DIR" == *"Dropbox"* ]]; then
    warn "This location appears to be inside a cloud-sync folder. See MIGRATION_PREP_2026-09.md's"
    warn "OneDrive/cloud-sync recommendation -- actively-written SQLite databases should not live"
    warn "inside a continuously-syncing folder."
else
    ok "Not inside a recognized cloud-sync path."
fi
echo

echo "=== Summary ==="
if [ "$FAILED" -eq 0 ]; then
    echo "All checks passed. Next: restore canonical state, then run ./verify_migration.sh"
else
    echo "One or more items above need attention before verify_migration.sh will pass cleanly."
fi
echo
echo "This script did NOT: create any canonical H/X, run Sleep, or launch Clark."
exit "$FAILED"
