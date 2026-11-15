# SPDX-License-Identifier: MIT
"""Logging for bst-whence, styled after BuildStream's own console output.

Messages are written to stderr as BuildStream-style console lines:

    [00:00:03][16:05:22] INFO    Analyzing app.bst
        detail lines are indented

Console verbosity follows BuildStream's convention:

* by default only warnings and errors are printed
* `--verbose` additionally shows informational and status messages
* `--debug` additionally shows debug messages

An optional log file (CLI: `--log-file`) receives informational messages and
above (or debug and above with `--debug`), regardless of the console level.
"""

import sys
import time
from contextlib import contextmanager

DEBUG = 10
INFO = 20
WARNING = 30
ERROR = 40

_LABELS = {
    DEBUG: "DEBUG",
    INFO: "INFO",
    WARNING: "WARNING",
    ERROR: "ERROR",
}


class Logger:
    """Line-oriented logger with bst-style formatting and optional file output."""

    def __init__(self):
        self._start_time = time.time()
        self._console_level = WARNING
        self._file = None
        self._file_level = None
        self._warned_once = set()

    def configure(self, verbose=False, debug=False, log_file=None):
        """(Re)configure console verbosity and optional file logging."""
        self.close()
        if debug:
            self._console_level = DEBUG
        elif verbose:
            self._console_level = INFO
        else:
            self._console_level = WARNING
        if log_file:
            self._file = open(log_file, "a", buffering=1)
            self._file_level = DEBUG if debug else INFO

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None
            self._file_level = None

    # ------------------------------------------------------------------
    # Message emitters
    # ------------------------------------------------------------------

    def debug(self, message, detail=None):
        self._emit(DEBUG, message, detail)

    def info(self, message, detail=None):
        self._emit(INFO, message, detail)

    def status(self, message, detail=None):
        """A progress message, shown with --verbose like BuildStream's status
        messages."""
        self._emit(INFO, message, detail, label="STATUS")

    def warning(self, message, detail=None):
        self._emit(WARNING, message, detail)

    def error(self, message, detail=None):
        self._emit(ERROR, message, detail)

    def warn_once(self, key, message, detail=None):
        """Emit a warning the first time it is triggered (keyed by `key`)."""
        if key in self._warned_once:
            return
        self._warned_once.add(key)
        self.warning(message, detail)

    @contextmanager
    def timed_activity(self, name, detail=None):
        """Log a STATUS message on entry and an INFO completion message with
        the elapsed time on exit."""
        self.status(name, detail)
        start = time.time()
        try:
            yield
        finally:
            self.info("{}: completed in {:.2f}s".format(name, time.time() - start))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _emit(self, level, message, detail=None, label=None):
        if label is None:
            label = _LABELS[level]
        text = self._format(label, message, detail)
        if level >= self._console_level:
            sys.stderr.write(text)
        if self._file is not None and level >= self._file_level:
            self._file.write(text)

    def _format(self, label, message, detail=None):
        elapsed = time.time() - self._start_time
        hours, remainder = divmod(int(elapsed), 3600)
        minutes, seconds = divmod(remainder, 60)
        wallclock = time.strftime("%H:%M:%S")
        lines = [
            "[{:02d}:{:02d}:{:02d}][{}] {:<7} {}".format(
                hours, minutes, seconds, wallclock, label, message
            )
        ]
        if detail:
            for line in str(detail).splitlines() or [""]:
                lines.append("    " + line)
        return "\n".join(lines) + "\n"


_logger = Logger()


def get_logger():
    """Return the process-wide Logger instance."""
    return _logger
