#!/usr/bin/env python3
"""Streaming redaction: private-key markers survive chunk boundaries."""

from __future__ import annotations

import unittest

from agentq.redaction import StreamingRedactor


class StreamingRedactionTests(unittest.TestCase):
    def test_marker_split_across_chunks_is_redacted(self) -> None:
        redactor = StreamingRedactor()
        out = redactor.feed("pre\n-----BEGIN PRIV")
        out += redactor.feed("ATE KEY-----\nbody\n-----END PRIVATE KEY-----\npost\n")
        out += redactor.finish()
        self.assertNotIn("body", out)
        self.assertIn("pre", out)
        self.assertIn("post", out)

    def test_unterminated_block_is_redacted_and_counted(self) -> None:
        redactor = StreamingRedactor()
        out = redactor.feed("before\n-----BEGIN PRIVATE KEY-----\nleak\n")
        out += redactor.finish()
        self.assertNotIn("leak", out)
        self.assertTrue(redactor.stats()["unterminated_private_key_blocks"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
