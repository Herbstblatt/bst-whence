#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
#
# Tests for text-report rendering, in particular the screen-fitting
# truncation of long ambiguous candidate lists.

import io
import unittest

from bst_whence import report


def make_entry(origin, matches, ambiguous=None):
    entry = {
        "path": "usr/lib/libx.so",
        "type": "file",
        "origin": origin,
        "matches": matches,
        "ambiguous": ambiguous if ambiguous is not None else len(matches) > 1,
    }
    return entry


def make_matches(names, exact=True):
    return [{"element": n, "path": "usr/lib/libx.so", "exact": exact} for n in names]


def many_deps(n):
    return ["dep{}.bst".format(i) for i in range(1, n + 1)]


class TestProvenanceText(unittest.TestCase):
    def test_own(self):
        entry = make_entry("own", [])
        self.assertEqual(report.provenance_text(entry), "(not from any dependency)")

    def test_single_match_untruncated(self):
        entry = make_entry("dependency", make_matches(["base.bst"]))
        text = report.provenance_text(entry, max_width=200)
        self.assertEqual(text, "base.bst")

    def test_fits_fully_shows_all_and_marker(self):
        entry = make_entry("dependency", make_matches(["a.bst", "b.bst"]))
        text = report.provenance_text(entry, max_width=200)
        self.assertEqual(text, "a.bst, b.bst  [ambiguous: 2 candidates]")

    def test_truncates_to_width(self):
        deps = many_deps(20)
        entry = make_entry("dependency", make_matches(deps))
        budget = 60
        text = report.provenance_text(entry, max_width=budget)
        self.assertIn("… (+", text)
        self.assertIn("more)", text)
        self.assertIn("dep1.bst", text)          # first candidate kept
        self.assertNotIn("dep20.bst", text)      # last candidate omitted
        self.assertLessEqual(len(text), budget + len(" … (+99 more)"))  # slack for suffix

    def test_keeps_at_least_one_candidate(self):
        deps = many_deps(20)
        entry = make_entry("dependency", make_matches(deps))
        text = report.provenance_text(entry, max_width=1)
        self.assertTrue(text.startswith("dep1.bst"))
        self.assertIn("… (+19 more)", text)

    def test_no_truncate_shows_all(self):
        deps = many_deps(20)
        entry = make_entry("dependency", make_matches(deps))
        text = report.provenance_text(entry, max_width=10, truncate=False)
        self.assertIn("dep1.bst", text)
        self.assertIn("dep20.bst", text)
        self.assertIn("[ambiguous: 20 candidates]", text)
        self.assertNotIn("more)", text)

    def test_moved_matches_truncate_too(self):
        deps = many_deps(15)
        matches = [
            {"element": n, "path": "usr/lib/{}.so".format(i), "exact": False}
            for i, n in enumerate(deps, start=1)
        ]
        entry = make_entry("moved", matches)
        text = report.provenance_text(entry, max_width=80)
        self.assertIn("… (+", text)
        self.assertIn("dep1.bst", text)

    def test_no_truncation_without_max_width(self):
        deps = many_deps(10)
        entry = make_entry("dependency", make_matches(deps))
        text = report.provenance_text(entry)
        self.assertIn("dep10.bst", text)
        self.assertIn("[ambiguous: 10 candidates]", text)


class TestRenderSection(unittest.TestCase):
    def make_result(self, n_deps):
        entries = []
        for i, name in enumerate(many_deps(n_deps), start=1):
            entries.append(make_entry("dependency", make_matches([name])))
        result = {
            "target": "app.bst",
            "element": "app.bst",
            "scope": "build",
            "artifact_digest": "0" * 64,
            "dependencies": many_deps(n_deps),
            "missing": [],
            "entries": entries,
            "summary": {
                "files": n_deps,
                "from_dependency": n_deps,
                "moved": 0,
                "ambiguous": 0,
                "unattributed": 0,
                "per_dependency": {},
            },
        }
        return result

    def test_renders(self):
        buf = io.StringIO()
        report.render_text_section(self.make_result(1), buf, by_dep=False,
                                   terminal_width=120)
        self.assertIn("dep1.bst", buf.getvalue())

    def test_lines_fit_terminal_width_when_ambiguous(self):
        deps = many_deps(30)
        entries = [make_entry("dependency", make_matches(deps))]  # one very ambiguous file
        result = self.make_result(1)
        result["entries"] = entries
        buf = io.StringIO()
        report.render_text_section(result, buf, by_dep=False, terminal_width=100)
        for line in buf.getvalue().splitlines():
            self.assertLessEqual(len(line), 100, repr(line))


if __name__ == "__main__":
    unittest.main(verbosity=2)
