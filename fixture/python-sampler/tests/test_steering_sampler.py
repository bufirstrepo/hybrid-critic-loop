import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from steering_sampler import SteeringSampler  # noqa: E402


class TestSteeringSampler(unittest.TestCase):
    def test_empty_average_is_zero(self):
        self.assertEqual(SteeringSampler().average(), 0.0)

    def test_single_push(self):
        s = SteeringSampler()
        s.push(-15.0)
        self.assertAlmostEqual(s.average(), -15.0)

    def test_average_of_several(self):
        s = SteeringSampler()
        for v in (-10.0, 0.0, 10.0):
            s.push(v)
        self.assertAlmostEqual(s.average(), 0.0)

    def test_capacity_wraparound(self):
        s = SteeringSampler()
        for i in range(130):
            s.push(float(i))
        self.assertAlmostEqual(s.average(), sum(range(10, 130)) / 120.0)

    def test_custom_capacity(self):
        s = SteeringSampler(capacity=3)
        for v in (1.0, 2.0, 3.0, 4.0):
            s.push(v)
        self.assertAlmostEqual(s.average(), 3.0)


if __name__ == "__main__":
    unittest.main()
