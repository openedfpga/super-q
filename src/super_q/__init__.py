"""super-q: ultra-fast distributed Quartus build system for Analogue Pocket cores.

Public API is intentionally small; most work goes through the CLI (`superq`)
or the MCP server (`super-q-mcp`). The modules below are importable for
programmatic use by agents embedding this in larger workflows.
"""

from super_q.project import PocketCore, detect_core
from super_q.scheduler import Scheduler
from super_q.seeds import SeedPlan, SeedResult
from super_q.timing import TimingReport

__version__ = "0.1.0"

__all__ = [
    "PocketCore",
    "detect_core",
    "Scheduler",
    "SeedPlan",
    "SeedResult",
    "TimingReport",
    "__version__",
]
