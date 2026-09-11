"""Make stdout/stderr safe for the box-drawing characters these CLIs print.

On Windows, Python picks the console code page for stdout — cp1252 when the
output is redirected to a file, a scheduled task log, or a service wrapper.
Every `print` containing an em dash, arrow or box-drawing rule then raises
UnicodeEncodeError and takes the process down at startup. `python dashboard.py
> server.log` was enough to kill the server before it bound its port.

Called from each entry point rather than at import time: a library has no
business reconfiguring a process's streams as a side effect of being imported.
"""

from __future__ import annotations

import sys


def use_utf8_stdout() -> None:
    """Switch stdout/stderr to UTF-8, degrading unmappable characters instead
    of raising. Safe to call more than once; a no-op where it does not apply."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # not a TextIOWrapper (pytest capture, some embeddings)
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Detached or already-closed stream. Printing is best effort.
            pass
