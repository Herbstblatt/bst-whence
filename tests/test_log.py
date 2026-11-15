#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
#
# Tests for the bst-whence logger: levels, formatting, one-time warnings
# and file logging.

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr

from bst_whence import log


class TestLogger(unittest.TestCase):
    def setUp(self):
        self.logger = log.Logger()
        self.addCleanup(self.logger.close)

    def capture(self, fn):
        buf = io.StringIO()
        with redirect_stderr(buf):
            fn()
        return buf.getvalue()

    def test_default_suppresses_info_status_and_debug(self):
        out = self.capture(
            lambda: (
                self.logger.debug("debug msg"),
                self.logger.info("info msg"),
                self.logger.status("status msg"),
            )
        )
        self.assertEqual(out, "")

    def test_default_shows_warnings_and_errors(self):
        out = self.capture(
            lambda: (self.logger.warning("warn msg"), self.logger.error("err msg"))
        )
        self.assertIn("WARNING", out)
        self.assertIn("warn msg", out)
        self.assertIn("ERROR", out)
        self.assertIn("err msg", out)

    def test_verbose_shows_info_and_status_but_not_debug(self):
        self.logger.configure(verbose=True)
        out = self.capture(
            lambda: (self.logger.info("info msg"), self.logger.status("doing work"))
        )
        self.assertIn("INFO", out)
        self.assertIn("info msg", out)
        self.assertIn("STATUS", out)
        self.assertIn("doing work", out)

        out = self.capture(lambda: self.logger.debug("debug msg"))
        self.assertEqual(out, "")

    def test_debug_shows_debug(self):
        self.logger.configure(debug=True)
        out = self.capture(lambda: self.logger.debug("debug msg"))
        self.assertIn("DEBUG", out)
        self.assertIn("debug msg", out)

    def test_format_has_elapsed_and_wallclock(self):
        out = self.capture(lambda: self.logger.warning("hello"))
        self.assertRegex(
            out, r"\[\d{2}:\d{2}:\d{2}\]\[\d{2}:\d{2}:\d{2}\] WARNING\s+hello"
        )

    def test_detail_is_indented(self):
        out = self.capture(
            lambda: self.logger.warning("head", detail="line one\nline two")
        )
        self.assertIn("    line one\n    line two", out)

    def test_warn_once(self):
        out = self.capture(
            lambda: (
                self.logger.warn_once("key", "first"),
                self.logger.warn_once("key", "second"),
            )
        )
        self.assertIn("first", out)
        self.assertNotIn("second", out)

    def test_file_logging_records_quiet_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log.txt")
            self.logger.configure(log_file=path)  # console stays quiet
            out = self.capture(lambda: self.logger.info("info msg"))
            self.assertEqual(out, "")
            self.logger.close()
            content = open(path).read()
            self.assertIn("INFO", content)
            self.assertIn("info msg", content)

    def test_file_logging_respects_debug_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log.txt")
            self.logger.configure(log_file=path)  # INFO+ in the file
            self.logger.debug("debug msg")
            self.logger.close()
            content = open(path).read()
            self.assertNotIn("debug msg", content)

            self.logger.configure(log_file=path, debug=True)  # DEBUG+ in the file
            self.logger.debug("debug msg")
            self.logger.close()
            content = open(path).read()
            self.assertIn("debug msg", content)

    def test_configure_closes_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path1 = os.path.join(tmp, "one.log")
            path2 = os.path.join(tmp, "two.log")
            self.logger.configure(log_file=path1)
            self.logger.configure(log_file=path2)
            self.logger.info("second run")
            self.logger.close()
            self.assertNotIn("second run", open(path1).read())
            self.assertIn("second run", open(path2).read())

    def test_timed_activity(self):
        self.logger.configure(verbose=True)
        out = self.capture(lambda: _run_activity(self.logger))
        self.assertIn("STATUS", out)
        self.assertIn("doing work", out)
        self.assertIn("completed in", out)


def _run_activity(logger):
    with logger.timed_activity("doing work"):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
