import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from speed_sampler import SpeedSampler  # noqa: E402


class TestSpeedSampler(unittest.TestCase):
    def test_empty_average_is_zero(self):
        self.assertEqual(SpeedSampler().average(), 0.0)

    def test_single_push(self):
        s = SpeedSampler()
        s.push(10.0)
        self.assertAlmostEqual(s.average(), 10.0)

    def test_average_of_several(self):
        s = SpeedSampler()
        for v in (2.0, 4.0, 6.0):
            s.push(v)
        self.assertAlmostEqual(s.average(), 4.0)

    def test_capacity_wraparound(self):
        s = SpeedSampler()
        for i in range(130):
            s.push(float(i))
        # Only the last 120 samples (10..129) count.
        self.assertAlmostEqual(s.average(), sum(range(10, 130)) / 120.0)

    def test_custom_capacity(self):
        s = SpeedSampler(capacity=3)
        for v in (1.0, 2.0, 3.0, 4.0):
            s.push(v)
        self.assertAlmostEqual(s.average(), 3.0)


if __name__ == "__main__":
    unittest.main()
