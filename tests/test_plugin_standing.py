"""The /critic slash handler serves `standing` like the CLI does.

plugin.py's hand-rolled slash dispatch has its own subcommand list;
this pins that `critic standing` prints hybrid_critic_loop.standing's
standing_report for the store bound at registration — the same text
`hermes critic standing` prints through cli.py.
"""

import os
import tempfile
import unittest
from unittest import mock

from hybrid_critic_loop import plugin
from hybrid_critic_loop.standing import standing_report
from tests.test_plugin import FakeCtx


class TestPluginStandingCommand(unittest.TestCase):
    def test_slash_standing_prints_the_standing_report(self):
        with mock.patch.dict(os.environ, {"HCL_DATA_DIR": tempfile.mkdtemp()}):
            ctx = FakeCtx()
            store = plugin.register(ctx)
            handler = ctx.commands["critic"]["handler"]
            out = handler("standing")
            self.assertEqual(out, standing_report(store))
            self.assertIn("standing — the client book:", out)
            self.assertIn("WINDOW SHOPPER", out)

    def test_help_and_unknown_subcommand_name_standing(self):
        with mock.patch.dict(os.environ, {"HCL_DATA_DIR": tempfile.mkdtemp()}):
            ctx = FakeCtx()
            plugin.register(ctx)
            handler = ctx.commands["critic"]["handler"]
            self.assertIn("critic standing", handler(""))
            self.assertIn("standing", handler("bogus"))


if __name__ == "__main__":
    unittest.main()
