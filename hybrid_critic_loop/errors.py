"""Exception hierarchy for the Hybrid Critic Loop plugin."""


class HCLError(Exception):
    """Base class for all Hybrid Critic Loop errors."""


class ContractError(HCLError):
    """Raised when a contract is malformed or violates contract rules."""


class BaselinePassedError(HCLError):
    """Raised when a baseline run unexpectedly passes.

    A baseline that already passes cannot prove a candidate fixed
    anything, so the loop must not proceed on it.
    """


class PanelError(HCLError):
    """Raised when the judging / scoring panel cannot complete."""


class GateError(HCLError):
    """Raised when a gate check fails or cannot be evaluated."""


class ExecutorUnavailable(HCLError):
    """Raised when a required expert executor is unavailable."""
