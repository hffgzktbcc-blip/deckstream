#!/usr/bin/env python3
"""Unit tests for list_files and RateLimiter in daemon/warmup.py."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "daemon"))
import warmup  # noqa: E402


class TestWarmup(unittest.TestCase):
    def test_list_files_skips_extensions_sorts_smallest_first_and_ignores_broken_links(self):
        with tempfile.TemporaryDirectory(prefix="ds_warmup_") as tmpdir:
            root = Path(tmpdir)
            (root / "sub").mkdir()
            f_large = root / "data.pak"
            f_small = root / "sub" / "config.ini"
            f_mid = root / "engine.dll"
            f_large.write_bytes(b"x" * 4096)
            f_small.write_bytes(b"x" * 64)
            f_mid.write_bytes(b"x" * 512)

            # Excluded extensions (case-insensitive)
            (root / "readme.TXT").write_bytes(b"hello")
            (root / "track.Mp3").write_bytes(b"music")
            (root / "sub" / "cover.PNG").write_bytes(b"img")

            # Unreadable broken symlink must be ignored without raising OSError
            os.symlink(root / "does_not_exist.bin", root / "broken.pak")

            skip = tuple(e.strip().lower() for e in warmup.DEFAULT_SKIP.split(",") if e.strip())
            got = warmup.list_files(str(root), skip)
            self.assertEqual(
                got,
                [
                    (64, str(f_small)),
                    (512, str(f_mid)),
                    (4096, str(f_large)),
                ],
            )

    def test_rate_limiter_zero_is_unlimited_and_positive_spaces_calls(self):
        unlimited = warmup.RateLimiter(0)
        t0 = time.monotonic()
        unlimited.take(10 ** 9)
        self.assertLess(time.monotonic() - t0, 0.05)

        # 1 MB/s = 1,048,576 bytes/s; taking 131,072 bytes (0.125 s) twice spaces the second call
        lim = warmup.RateLimiter(1.0)
        chunk = 128 * 1024
        t0 = time.monotonic()
        lim.take(chunk)
        first_free = lim.next_free
        self.assertGreaterEqual(first_free - t0, 0.10)
        lim.take(chunk)
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.10)
        self.assertGreaterEqual(lim.next_free - first_free, 0.11)


if __name__ == "__main__":
    unittest.main()
