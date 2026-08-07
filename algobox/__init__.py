"""AlgoBox — eight order-flow modules, each in a simple and a tick-level version.

    from algobox import analyze
    analyze("NQ=F", demo=True)            # both engines + their disagreement
    analyze("NQ=F", engine="simple")      # bars only, fast, works anywhere
    analyze("NQ=F", engine="tick")        # the tape

Terminal:  python3 -m algobox --demo
Dashboard: GET /api/algobox
"""

from .core import SIMPLE, TICK, Context, Event, Module, Reading, Tick
from .suite import (
    MODULES, SuiteResult, analyze, build_context, compare, run, run_both,
)
from .ticks import build_footprint, get_ticks, load_tick_file, reconstruct

__all__ = [
    "SIMPLE", "TICK", "Context", "Event", "Module", "Reading", "Tick",
    "MODULES", "SuiteResult", "analyze", "build_context", "compare", "run",
    "run_both", "build_footprint", "get_ticks", "load_tick_file", "reconstruct",
]
