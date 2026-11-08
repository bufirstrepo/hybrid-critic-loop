"""Exception hierarchy for the Hybrid Critic Loop plugin."""


class HCLError(Exception):
    """Base class for all Hybrid Critic Loop errors."""


class ContractError(HCLError):
    """Raised when a contract is malformed or violates contract rules."""


class PanelError(HCLError):
    """Raised when the judging / scoring panel cannot complete."""


class GateError(HCLError):
    """Raised when a gate check fails or cannot be evaluated."""


# A vacuous baseline is a refusal result, not an exception, and an absent
# seat is an ``unavailable`` GenResult — the classes that once named them
# were raised nowhere and are gone (fix pass Wave 2.10, review 1C-18).
