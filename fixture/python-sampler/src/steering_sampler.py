"""SteeringSampler -- rolling average of steering angle, one push per tick.

Same shape as SpeedSampler (fixed capacity 120, average() == 0.0 when
empty). Stub: both methods raise NotImplementedError until implemented.
"""


class SteeringSampler:
    CAPACITY = 120

    def __init__(self, capacity: int = CAPACITY):
        self._capacity = capacity

    def push(self, angle_deg: float) -> None:
        raise NotImplementedError

    def average(self) -> float:
        raise NotImplementedError
