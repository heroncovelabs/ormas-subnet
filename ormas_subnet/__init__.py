"""ormas-subnet — the Ormas miner protocol, client, and reference skeleton.

Not our miner. See ``docs/DECISIONS.md`` and ``README.md`` for what "miner" means here and
what this package deliberately does not ship.
"""
from .client import OrmasMinerClient, load_token
from .protocol import (
    RUNNER_DEVICE_HEADER,
    RUNNER_PROTOCOL_V1,
    VERIFICATION_STATES,
    RepoRegistration,
    RunnerRegistration,
    TaskDraft,
    TaskEvent,
    TaskLease,
    TaskReceipt,
    TaskTerminal,
)
from .skeleton import MinerConfig, MinerSkeleton, SolveFn, SolveResult

__version__ = "0.0.1"

__all__ = [
    "__version__",
    "OrmasMinerClient",
    "load_token",
    "RUNNER_DEVICE_HEADER",
    "RUNNER_PROTOCOL_V1",
    "VERIFICATION_STATES",
    "RepoRegistration",
    "RunnerRegistration",
    "TaskDraft",
    "TaskEvent",
    "TaskLease",
    "TaskReceipt",
    "TaskTerminal",
    "MinerConfig",
    "MinerSkeleton",
    "SolveFn",
    "SolveResult",
]
