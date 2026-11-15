# SPDX-License-Identifier: MIT
"""Text and JSON rendering of bst-whence analysis results."""

import json
from collections import defaultdict

PROVENANCE_NOTES = (
    "provenance notes:\n"
    "  (not from any dependency)   produced by this element's own build or sources\n"
    "  X (as P)                    content identical to path `P` in dependency X\n"
    "  [ambiguous: N]              identical content found in N dependencies"
)


def provenance_text(entry):
    if entry["origin"] == "own":
        return "(not from any dependency)"
    parts = []
    for m in entry["matches"]:
        if m["exact"]:
            parts.append(m["element"])
        else:
            parts.append("{} (as {})".format(m["element"], m["path"]))
    text = ", ".join(parts)
    if entry["ambiguous"]:
        text += "  [ambiguous: {} candidates]".format(len(entry["matches"]))
    return text


def render_text_section(result, out, by_dep, show_summary=True):
    if "error" in result:
        out.write("target: {}\n".format(result["target"]))
        out.write("error: {}\n".format(result["error"]))
        return

    out.write("target: {} ({})\n".format(result["element"], result["target"]))
    out.write("dependencies considered ({} scope): {}\n".format(result["scope"], len(result["dependencies"])))
    out.write("artifact files digest: {}\n".format(result["artifact_digest"]))
    if result["missing"]:
        out.write("missing dependency artifacts:\n")
        for m in result["missing"]:
            out.write("  {} — {}\n".format(m["name"], m["reason"]))
    out.write("\n")
    out.write(PROVENANCE_NOTES + "\n\n")

    width = max([len(e["path"]) for e in result["entries"]] + [4])

    rows = result["entries"]
    if by_dep:
        groups = defaultdict(list)
        for entry in rows:
            key = entry["matches"][0]["element"] if entry["matches"] else "(not from any dependency)"
            groups[key].append(entry)
        ordered = sorted(groups.items(), key=lambda kv: kv[0].lower())
    else:
        ordered = [("", rows)]

    first = True
    for group_name, entries in ordered:
        if by_dep:
            if not first:
                out.write("\n")
            out.write("from {} ({} file{}):\n".format(group_name, len(entries), "" if len(entries) == 1 else "s"))
            first = False
        for entry in entries:
            out.write("  {:<{}}  ←  {}\n".format(entry["path"], width, provenance_text(entry)))

    s = result["summary"]
    if not show_summary:
        return
    out.write("\nsummary:\n")
    out.write("  files:                                   {}\n".format(s["files"]))
    out.write("  from a dependency (same path):           {}\n".format(s["from_dependency"]))
    out.write("  from a dependency (different path):      {}\n".format(s["moved"]))
    out.write("  ambiguous (several possible deps):       {}\n".format(s["ambiguous"]))
    out.write("  not attributable to any dependency:      {}\n".format(s["unattributed"]))
    if s["per_dependency"]:
        out.write("  files per dependency:\n")
        for name, count in s["per_dependency"].items():
            out.write("    {:>5}  {}\n".format(count, name))


def render_text(results, out, by_dep, show_summary=True):
    for i, result in enumerate(results):
        if i:
            out.write("\n" + "=" * 78 + "\n\n")
        render_text_section(result, out, by_dep, show_summary=show_summary)


def render_json(results):
    return json.dumps({r["target"]: r for r in results}, indent=2)
