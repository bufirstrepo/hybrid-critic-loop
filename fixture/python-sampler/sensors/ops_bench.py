"""Measured-gate sensor: per-tick arithmetic work of a sampler hot loop.

Usage: python3 sensors/ops_bench.py <module_path> <ClassName>

Loads the candidate's sampler class, warms it up, then runs a fixed
number of ticks (push + average per tick, the way a physics loop reads
the value every tick) with an instrumented float type that counts every
addition. Prints one JSON object: {"ops_per_tick": <float>}.

A recomputing implementation (sum over the whole buffer per tick)
scores ~capacity ops/tick. An O(1) implementation (preallocated ring
buffer + running total) scores ~2. The metric is exact integer
counting -- deterministic, no wall-clock involved.

Known limitation (stated plainly): the count instruments arithmetic
performed on the values the sampler stores. An implementation that
converts each pushed value to a plain float at push time performs its
later arithmetic on plain floats, which this sensor cannot see; such
an implementation would under-report. The C# fixture's sensor
(alloc_bytes_per_tick via GC.GetAllocatedBytesForCurrentThread) has
no equivalent blind spot. The local demo solvers store values as
given, so the counts in the demo are exact for the code that ran.
"""

import importlib.util
import json
import sys
from pathlib import Path

TICKS = 600
WARMUP = 240


class CountingFloat(float):
    ops = 0

    def __add__(self, other):
        CountingFloat.ops += 1
        return CountingFloat(float(self) + float(other))

    def __radd__(self, other):
        CountingFloat.ops += 1
        return CountingFloat(float(other) + float(self))


def load_class(module_path: str, class_name: str):
    spec = importlib.util.spec_from_file_location("candidate_sampler", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


def main() -> int:
    module_path, class_name = sys.argv[1], sys.argv[2]
    cls = load_class(module_path, class_name)
    sampler = cls()
    for i in range(WARMUP):
        sampler.push(CountingFloat(i * 0.5))
        sampler.average()
    CountingFloat.ops = 0
    for i in range(TICKS):
        sampler.push(CountingFloat(i * 0.25))
        sampler.average()
    print(json.dumps({"ops_per_tick": CountingFloat.ops / TICKS}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
