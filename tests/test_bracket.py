"""Bracket unit tests: the scored step-down ladder (owner correction).

Pure-function tests over bracket.py — ladder sizes for both cut
modes, the never-jump-past-two rule, cut-line reporting, and the tie
rule (fewer tokens, then earlier completion).
"""

import unittest

from hybrid_critic_loop import bracket as bracket_mod

FIXED2 = {"cut_mode": "fixed", "cut_per_round": 2}
HALF = {"cut_mode": "half", "cut_per_round": 2}


def _ladder(start, config):
    sizes = [start]
    while sizes[-1] > 1:
        sizes.append(bracket_mod.next_field_size(sizes[-1], config))
    return sizes


class TestLadders(unittest.TestCase):
    def test_fixed_default_ladder_from_16(self):
        self.assertEqual(
            _ladder(16, FIXED2), [16, 14, 12, 10, 8, 6, 4, 2, 1]
        )

    def test_fixed_ladder_never_jumps_past_two(self):
        # 5 - 2 = 3, then 3 - 2 = 1 would skip the final two: cut to 2.
        self.assertEqual(_ladder(5, FIXED2), [5, 3, 2, 1])
        self.assertEqual(_ladder(3, FIXED2), [3, 2, 1])
        self.assertEqual(_ladder(2, FIXED2), [2, 1])
        self.assertEqual(_ladder(1, FIXED2), [1])

    def test_half_ladder_rounds_up_for_odd_fields(self):
        self.assertEqual(_ladder(9, HALF), [9, 5, 3, 2, 1])
        self.assertEqual(_ladder(16, HALF), [16, 8, 4, 2, 1])
        self.assertEqual(_ladder(4, HALF), [4, 2, 1])

    def test_cut_per_round_one(self):
        cfg = {"cut_mode": "fixed", "cut_per_round": 1}
        self.assertEqual(_ladder(4, cfg), [4, 3, 2, 1])

    def test_bracket_config_defaults_and_bad_values(self):
        self.assertEqual(
            bracket_mod.bracket_config({}),
            {"cut_mode": "fixed", "cut_per_round": 2},
        )
        self.assertEqual(
            bracket_mod.bracket_config({"bracket": {"cut_mode": "half"}}),
            {"cut_mode": "half", "cut_per_round": 2},
        )
        self.assertEqual(
            bracket_mod.bracket_config({"bracket": {"cut_mode": "weird",
                                                     "cut_per_round": 0}}),
            {"cut_mode": "fixed", "cut_per_round": 2},
        )


class TestApplyCut(unittest.TestCase):
    def _field(self, ids):
        return [{"id": cid} for cid in ids]

    def test_cut_keeps_top_scores_and_reports_cut_line(self):
        field = self._field(["c1", "c2", "c3", "c4", "c5"])
        validity = {"c1": 9.0, "c2": 8.0, "c3": 7.0, "c4": 6.0, "c5": 5.0}
        cut = bracket_mod.apply_cut(field, validity, FIXED2)
        self.assertEqual([c["id"] for c in cut["advanced"]], ["c1", "c2", "c3"])
        self.assertEqual([c["id"] for c in cut["eliminated"]], ["c4", "c5"])
        self.assertEqual(cut["cut_line"], 7.0)

    def test_no_cut_reports_no_cut_line(self):
        field = self._field(["c1"])
        cut = bracket_mod.apply_cut(field, {"c1": 9.0}, FIXED2)
        self.assertEqual([c["id"] for c in cut["advanced"]], ["c1"])
        self.assertEqual(cut["eliminated"], [])
        self.assertIsNone(cut["cut_line"])

    def test_tie_at_cut_line_fewer_tokens_wins(self):
        field = [
            {"id": "heavy", "tokens_in": 900, "tokens_out": 100},
            {"id": "light", "tokens_in": 10, "tokens_out": 5},
            {"id": "mid", "tokens_in": 100, "tokens_out": 100},
        ]
        validity = {"heavy": 8.0, "light": 8.0, "mid": 8.0}
        cut = bracket_mod.apply_cut(field, validity, FIXED2)
        # target = 2 (3 - 2 would jump past the final two)
        self.assertEqual([c["id"] for c in cut["advanced"]], ["light", "mid"])
        self.assertEqual([c["id"] for c in cut["eliminated"]], ["heavy"])

    def test_tie_then_earlier_completion_wins(self):
        field = [
            {"id": "slow", "tokens_in": None, "tokens_out": None},
            {"id": "fast", "tokens_in": None, "tokens_out": None},
        ]
        validity = {"slow": 7.0, "fast": 7.0}
        cut = bracket_mod.apply_cut(
            field, validity, FIXED2, durations={"slow": 5000, "fast": 1200}
        )
        self.assertEqual([c["id"] for c in cut["advanced"]], ["fast"])
        self.assertEqual([c["id"] for c in cut["eliminated"]], ["slow"])

    def test_unscored_candidate_sorts_last(self):
        field = self._field(["scored", "unscored"])
        cut = bracket_mod.apply_cut(field, {"scored": 1.0}, FIXED2)
        self.assertEqual([c["id"] for c in cut["advanced"]], ["scored"])


if __name__ == "__main__":
    unittest.main()
