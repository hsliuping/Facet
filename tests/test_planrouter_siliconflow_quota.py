"""facet-siliconflow-quota: curated free-model rows as freealign JSON."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from facet.gateway import siliconflow_quota as sfq
from facet.gateway.core import FREE_RESERVE_DEFAULT_TOKENS
from facet.gateway.freealign import free_models_from_rows


class RowsTests(unittest.TestCase):
    def test_rows_pass_the_free_align_filters(self):
        rows = sfq.rows_from_models()
        self.assertEqual([r["model"] for r in rows], list(sfq.FREE_MODELS))
        for row in rows:
            self.assertEqual(row["type"], "Text")
            # The sentinel allowance must survive both the sweep's reserve
            # margin and the console probe path (reserve=0).
            self.assertGreater(row["remaining"], FREE_RESERVE_DEFAULT_TOKENS)
            self.assertGreaterEqual(row["expires"], "2026-01-01")
        self.assertEqual(
            free_models_from_rows(rows, reserve=FREE_RESERVE_DEFAULT_TOKENS),
            list(sfq.FREE_MODELS))

    def test_cli_prints_bare_json_array(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sfq.main(["--output", "json"])
        self.assertEqual(code, 0)
        rows = json.loads(out.getvalue())
        self.assertEqual([r["model"] for r in rows], list(sfq.FREE_MODELS))


if __name__ == "__main__":
    unittest.main()
