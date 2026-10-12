"""Windows->macOS portability repair (migration prep, 2026-09-06).

A single, correct construction for a read-only SQLite URI, used
everywhere a connection must never create a missing database file and
must never accept a write. Several call sites across this codebase
used a naive f"file:{db_path}?mode=ro" string interpolation -- this is
exactly the fragile pattern that produced the interrupted substrate
comparison's own pilot bug (its filesystem guard rejected that shape
for a temp fixture, silently causing "not established" temporal facts
instead of a hard failure). pathlib.Path.as_uri() is the standard-
library, RFC 8089-correct way to build a file: URI on any platform
(Windows, macOS, Linux alike) -- percent-encoding included -- so this
is a portability and correctness fix at once, not merely a macOS
accommodation."""
from pathlib import Path


def readonly_sqlite_uri(db_path: str) -> str:
    """Returns a `file:...?mode=ro` URI for `db_path`, suitable for
    sqlite3.connect(..., uri=True). Never creates a missing file;
    never accepts a write through the resulting connection."""
    return Path(db_path).resolve().as_uri() + "?mode=ro"
