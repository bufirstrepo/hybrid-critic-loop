#!/usr/bin/env python3
"""Standalone deterministic dev solver (stdlib only).

Reads JSON on stdin: {"contract": {...}, "context": str}
Scans cwd for the contract's target file, reads stub for class name,
writes an implementation (naive or ring strategy).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CANDIDATES = ["speed_sampler.py", "steering_sampler.py", "SpeedSampler.cs", "SteeringSampler.cs"]


def find_target(cwd: Path, contract: dict) -> Path | None:
    target = contract.get("target_file") if isinstance(contract, dict) else None
    if target:
        p = Path(target)
        if not p.is_absolute():
            p = cwd / p
        if p.exists():
            return p
        # If target specified but not found directly, search by name
        name = Path(target).name
        for found in cwd.rglob(name):
            return found
        # Still return the intended path if its parent exists? Prefer search fallback
        # If not found at all, treat as missing unless parent dir exists and we should create?
        # Spec: exit 2 if no target file found, so return None here if not exists
        return None
    # Search candidates: first direct children, then recursive
    for name in CANDIDATES:
        direct = cwd / name
        if direct.exists():
            return direct
    for name in CANDIDATES:
        for found in cwd.rglob(name):
            return found
    return None


def derive_class_name(filename: str, content: str) -> str:
    m = re.search(r"class\s+(\w+)", content or "")
    if m:
        return m.group(1)
    # Derive from filename
    stem = Path(filename).stem
    # snake_case -> PascalCase
    parts = re.split(r"[_\-\s]+", stem)
    if parts and all(p for p in parts):
        derived = "".join(p[:1].upper() + p[1:] for p in parts)
        if derived:
            return derived
    return stem or "Sampler"


PYTHON_NAIVE = '''class {cls}:
    """Naive sampler: list + slicing trim, sum/len average."""

    def __init__(self, capacity=120):
        self.capacity = int(capacity) if capacity else 120
        self._capacity = self.capacity
        self._samples = []

    def push(self, value):
        self._samples.append(value)
        if len(self._samples) > self.capacity:
            self._samples = self._samples[-self.capacity:]

    # Aliases for compatibility
    def add(self, value):
        self.push(value)

    def sample(self, value):
        self.push(value)

    def record(self, value):
        self.push(value)

    def average(self):
        if not self._samples:
            return 0.0
        return sum(self._samples) / len(self._samples)

    def mean(self):
        return self.average()

    def get_average(self):
        return self.average()

    @property
    def count(self):
        return len(self._samples)

    def __len__(self):
        return len(self._samples)

    @property
    def samples(self):
        return list(self._samples)

    def clear(self):
        self._samples = []
'''

PYTHON_RING = '''class {cls}:
    """Ring sampler: preallocated buffer, running total, O(1) push."""

    def __init__(self, capacity=120):
        self.capacity = int(capacity) if capacity else 120
        self._capacity = self.capacity
        self._buffer = [0.0] * self.capacity
        self._samples = self._buffer  # alias for introspection compat
        self._index = 0
        self._count = 0
        self._total = 0.0

    def push(self, value):
        v = value
        if self._count < self.capacity:
            self._buffer[self._index] = v
            self._total += v
            self._count += 1
        else:
            old = self._buffer[self._index]
            self._total += v - old
            self._buffer[self._index] = v
        self._index = (self._index + 1) % self.capacity

    def add(self, value):
        self.push(value)

    def sample(self, value):
        self.push(value)

    def record(self, value):
        self.push(value)

    def average(self):
        if self._count == 0:
            return 0.0
        return self._total / self._count

    def mean(self):
        return self.average()

    def get_average(self):
        return self.average()

    @property
    def count(self):
        return self._count

    def __len__(self):
        return self._count

    @property
    def samples(self):
        if self._count < self.capacity:
            return list(self._buffer[:self._count])
        # Oldest first: start at _index
        return [self._buffer[(self._index + i) % self.capacity] for i in range(self.capacity)]

    def clear(self):
        self._buffer = [0.0] * self.capacity
        self._samples = self._buffer
        self._index = 0
        self._count = 0
        self._total = 0.0
'''

PYTHON_BALANCED = '''from collections import deque


class {cls}:
    """Balanced sampler: deque-backed ring with a fixed maximum length.

    A middle shape between the naive list (which re-slices on trim) and
    the preallocated ring: the deque enforces the capacity bound itself,
    evicting the oldest sample on push once full.
    """

    def __init__(self, capacity=120):
        self.capacity = int(capacity) if capacity else 120
        self._capacity = self.capacity
        self._window = deque(maxlen=self.capacity)

    def push(self, value):
        self._window.append(value)

    # Aliases for compatibility
    def add(self, value):
        self.push(value)

    def sample(self, value):
        self.push(value)

    def record(self, value):
        self.push(value)

    def average(self):
        if not self._window:
            return 0.0
        total = 0.0
        for value in self._window:
            total += value
        return total / len(self._window)

    def mean(self):
        return self.average()

    def get_average(self):
        return self.average()

    @property
    def count(self):
        return len(self._window)

    def __len__(self):
        return len(self._window)

    @property
    def samples(self):
        return list(self._window)

    def clear(self):
        self._window.clear()
'''

CS_NAIVE = '''using System;
using System.Collections.Generic;

public class {cls}
{{
    private readonly int _capacity;
    private readonly List<float> _samples = new List<float>();

    public {cls}(int capacity = 120)
    {{
        _capacity = capacity > 0 ? capacity : 120;
    }}

    public int Count => _samples.Count;
    public int Capacity => _capacity;

    public void Push(float value)
    {{
        _samples.Add(value);
        if (_samples.Count > _capacity)
        {{
            _samples.RemoveRange(0, _samples.Count - _capacity);
        }}
    }}

    public void Add(float value) => Push(value);
    public void Sample(float value) => Push(value);

    public float Average()
    {{
        if (_samples.Count == 0) return 0f;
        double sum = 0;
        foreach (var v in _samples) sum += v;
        return (float)(sum / _samples.Count);
    }}

    public float Mean() => Average();

    public void Clear() => _samples.Clear();
}}
'''

CS_RING = '''using System;

public class {cls}
{{
    private readonly int _capacity;
    private readonly float[] _buffer;
    private int _index;
    private int _count;
    private double _total;

    public {cls}(int capacity = 120)
    {{
        _capacity = capacity > 0 ? capacity : 120;
        _buffer = new float[_capacity];
        _index = 0;
        _count = 0;
        _total = 0;
    }}

    public int Count => _count;
    public int Capacity => _capacity;

    public void Push(float value)
    {{
        if (_count < _capacity)
        {{
            _buffer[_index] = value;
            _total += value;
            _count++;
        }}
        else
        {{
            _total += value - _buffer[_index];
            _buffer[_index] = value;
        }}
        _index = (_index + 1) % _capacity;
    }}

    public void Add(float value) => Push(value);
    public void Sample(float value) => Push(value);

    public float Average()
    {{
        if (_count == 0) return 0f;
        return (float)(_total / _count);
    }}

    public float Mean() => Average();

    public void Clear()
    {{
        Array.Clear(_buffer, 0, _buffer.Length);
        _index = 0;
        _count = 0;
        _total = 0;
    }}
}}
'''


CS_BALANCED = '''using System;
using System.Collections.Generic;

public class {cls}
{{
    private readonly int _capacity;
    private readonly Queue<float> _window = new Queue<float>();

    public {cls}(int capacity = 120)
    {{
        _capacity = capacity > 0 ? capacity : 120;
    }}

    public int Count => _window.Count;
    public int Capacity => _capacity;

    public void Push(float value)
    {{
        _window.Enqueue(value);
        while (_window.Count > _capacity)
        {{
            _window.Dequeue();
        }}
    }}

    public void Add(float value) => Push(value);
    public void Sample(float value) => Push(value);

    public float Average()
    {{
        if (_window.Count == 0) return 0f;
        double sum = 0;
        foreach (var v in _window) sum += v;
        return (float)(sum / _window.Count);
    }}

    public float Mean() => Average();

    public void Clear() => _window.Clear();
}}
'''


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default="naive", choices=["naive", "ring", "balanced"])
    args = parser.parse_args(argv)

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        print(f"invalid JSON on stdin: {exc}", file=sys.stderr)
        return 2

    contract = payload.get("contract", {}) if isinstance(payload, dict) else {}
    if not isinstance(contract, dict):
        contract = {}
    # context unused for implementation choice here; strategy comes from CLI
    cwd = Path.cwd()
    target = find_target(cwd, contract)
    if target is None:
        print("no target file found", file=sys.stderr)
        return 2

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception:
        content = ""
    cls = derive_class_name(target.name, content)
    is_cs = target.suffix.lower() == ".cs"
    if is_cs:
        template = {"ring": CS_RING, "balanced": CS_BALANCED}.get(args.strategy, CS_NAIVE)
    else:
        template = {"ring": PYTHON_RING, "balanced": PYTHON_BALANCED}.get(args.strategy, PYTHON_NAIVE)
    impl = template.format(cls=cls)

    target.write_text(impl, encoding="utf-8")
    summary = {"solver": "dev_solver", "strategy": args.strategy, "file": target.name, "class": cls}
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
