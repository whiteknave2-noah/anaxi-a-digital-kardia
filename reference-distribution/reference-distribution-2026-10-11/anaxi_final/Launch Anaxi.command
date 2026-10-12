#!/bin/bash
# macOS equivalent of "Launch Anaxi.bat" -- byte-for-byte same contract:
# refuse to start without the authenticated human binding, always run
# from this file's own directory (so a Finder alias/shortcut can sit
# anywhere), invoke the SAME llama_launch.py browser-first entrypoint
# with the SAME --conversation default, and keep the Terminal window
# open afterward so a fast failure is readable rather than flashing by.
#
# Does not silently run Sleep. Does not silently change the production
# model mapping. Any startup failure from llama_launch.py/llama_gui.py
# propagates to this window unmodified.
set -u

if [ "${ANAXI_BOUND_HUMAN_ACTOR_ID:-}" != "human-actor-c8feddc1b4bb" ]; then
    echo "Required authenticated human binding is missing or incorrect. ANAXI was not started."
    echo "(Set ANAXI_BOUND_HUMAN_ACTOR_ID=human-actor-c8feddc1b4bb in your shell profile.)"
    read -n 1 -s -r -p "Press any key to close..."
    exit 1
fi

# Resolve this script's own directory, exactly like %~dp0 on Windows --
# a double-click via Finder, an alias, or a symlink must all still
# land in the real project folder, never the caller's cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Use the project-local venv's own interpreter, never whatever "python3"
# happens to resolve to on PATH -- bootstrap_macos.sh installs the
# accepted dependency set (gradio, etc.) only into .venv-macos, and a
# system/PATH python3 will not have them. Resolved as an absolute path
# from this script's own directory, so this does not depend on the
# venv being manually activated first or on the caller's cwd.
PYTHON_BIN="$SCRIPT_DIR/.venv-macos/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "Project venv not found or not executable at: $PYTHON_BIN"
    echo "Run bootstrap_macos.sh first to create .venv-macos and install dependencies."
    read -n 1 -s -r -p "Press any key to close..."
    exit 1
fi

# The waking path is deliberately offline.  Bootstrap is the only operation
# allowed to fetch the pinned embedder; an ordinary launch must positively
# establish that the local artifact is already present and dimensionally
# compatible before it touches resource state or starts the backend.
"$PYTHON_BIN" prepare_embedding_model.py --check-only
EMBEDDING_STATUS=$?
if [ "$EMBEDDING_STATUS" -ne 0 ]; then
    echo "The required local embedding model is absent or invalid. ANAXI was not started."
    echo "Run bootstrap_macos.sh while setup networking is explicitly intended, then launch again."
    read -n 1 -s -r -p "Press any key to close..."
    exit "$EMBEDDING_STATUS"
fi

# Compile the tiny built-in PDFKit page renderer once.  Runtime page access
# invokes this fixed local binary; it never executes content embedded in a PDF.
PDF_RENDERER_SOURCE="$SCRIPT_DIR/pdf_page_renderer.swift"
PDF_RENDERER_BIN="$SCRIPT_DIR/.venv-macos/bin/anaxi-pdf-page-renderer"
if [ ! -x "$PDF_RENDERER_BIN" ] || [ "$PDF_RENDERER_SOURCE" -nt "$PDF_RENDERER_BIN" ]; then
    CLANG_MODULE_CACHE_PATH="${TMPDIR:-/tmp}/anaxi-clang-module-cache" \
    SWIFT_MODULECACHE_PATH="${TMPDIR:-/tmp}/anaxi-swift-module-cache" \
    /usr/bin/xcrun swiftc "$PDF_RENDERER_SOURCE" -o "$PDF_RENDERER_BIN"
    RENDERER_STATUS=$?
    if [ "$RENDERER_STATUS" -ne 0 ]; then
        echo "The bounded PDF page renderer could not be prepared. ANAXI was not started."
        read -n 1 -s -r -p "Press any key to close..."
        exit "$RENDERER_STATUS"
    fi
fi

# Capability-completeness invariant: the human-facing public collection and
# the live Workspace are separate stores, so reconcile the explicitly
# classified public Books/Pictures/Music resources before every launch.  The
# ingest is idempotent, source-preserving, and fail-closed on a destination
# conflict; it never traverses Private Space, Archive, or legacy journals.
"$PYTHON_BIN" public_resource_ingest.py sync
INGEST_STATUS=$?
if [ "$INGEST_STATUS" -ne 0 ]; then
    echo "Public resource ingest did not complete safely. ANAXI was not started."
    read -n 1 -s -r -p "Press any key to close..."
    exit "$INGEST_STATUS"
fi

"$PYTHON_BIN" llama_launch.py --conversation
STATUS=$?

echo
echo "(llama_launch.py exited with status $STATUS)"
read -n 1 -s -r -p "Press any key to close..."
exit "$STATUS"
