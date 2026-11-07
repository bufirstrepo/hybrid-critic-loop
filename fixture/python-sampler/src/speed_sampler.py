"""SpeedSampler -- rolling average of vehicle speed, one push per physics tick.

Stub: both methods raise NotImplementedException-equivalent until a
candidate implements them. Contract: fixed capacity of 120 samples;
average() returns 0.0 when empty.
"""


class SpeedSampler:
    CAPACITY = 120

    def __init__(self, capacity: int = CAPACITY):
        self._capacity = capacity

    def push(self, mps: float) -> None:
        raise NotImplementedError

    def average(self) -> float:
        raise NotImplementedError
