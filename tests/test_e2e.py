#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
#
# End-to-end test for bst-whence without needing casd or a real build:
# it fabricates the exact artifact-cache state a `bst build` would have
# produced (artifact metadata protos + CAS blobs in bst's on-disk layout)
# and runs the tool's full pipeline: project load, element load, state
# initialization, artifact metadata lookup, CAS walking and attribution.
#
# Run with the BuildStream 2 Python environment available:
#   <venv>/bin/python tests/test_e2e.py

import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import bst_whence.core as core
from bst_whence import cli

from buildstream._exceptions import CASCacheError
from buildstream._protos.build.bazel.remote.execution.v2 import remote_execution_pb2
from buildstream._protos.buildstream.v2 import artifact_pb2

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE_DIR = os.path.join(HERE, "..", "example")


class CasWriter:
    """Writes blobs into bst's on-disk CAS layout: <casdir>/objects/<aa>/<rest>."""

    def __init__(self, casdir):
        self.casdir = casdir

    def objpath(self, digest):
        return os.path.join(self.casdir, "objects", digest.hash[:2], digest.hash[2:])

    def put(self, data):
        digest = remote_execution_pb2.Digest()
        digest.hash = hashlib.sha256(data).hexdigest()
        digest.size_bytes = len(data)
        path = self.objpath(digest)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return digest

    def make_directory(self, files=(), symlinks=()):
        directory = remote_execution_pb2.Directory()
        for name, content in files:
            fnode = directory.files.add()
            fnode.name = name
            fnode.digest.CopyFrom(self.put(content))
        for name, target in symlinks:
            snode = directory.symlinks.add()
            snode.name = name
            snode.target = target
        return self.put(directory.SerializeToString())


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.addCleanup(lambda: core.release_cas_resources(self.context))

        # Isolate bst's XDG dirs inside the temp dir.
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.tmp, "xdg-cache")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "xdg-config")
        os.makedirs(os.environ["XDG_CONFIG_HOME"], exist_ok=True)

        from buildstream._context import Context

        self.context = Context()
        self.context.load(config=None)

        self.project = core.load_project(self.context, EXAMPLE_DIR)

        self.casdir = os.path.join(self.context.cachedir, "cas")
        self.writer = CasWriter(self.casdir)
        os.makedirs(os.path.join(self.casdir, "objects"), exist_ok=True)
        os.makedirs(self.context.artifactdir, exist_ok=True)

        self.base_tree = self.writer.make_directory(
            files=[
                ("usr/share/base.conf", b"base-config\n"),
            ],
            symlinks=[
                ("usr/share/current.conf", "base.conf"),
            ],
        )
        self.lib_tree = self.writer.make_directory(
            files=[
                ("usr/lib/libx.so", b"BINARYX"),
                ("usr/lib/liby.so", b"BINARYY"),
            ]
        )
        self.app_tree = self.writer.make_directory(
            files=[
                ("usr/bin/app", b"APP"),
                ("usr/share/base.conf", b"base-config\n"),    # copied from base
                ("usr/lib/libx.so", b"BINARYX"),              # copied from lib
                ("opt/renamed-liby.so", b"BINARYY"),          # copied+renamed from lib
                ("usr/bin/app-stripped", b"APP-modified"),    # modified after copy
            ],
            symlinks=[
                ("usr/share/current.conf", "base.conf"),      # copied from base
            ],
        )

    def fabricate(self, target_name, tree_digest):
        """Write the artifact metadata proto where bst would keep it."""
        elements = self.project.load_elements([target_name])
        element = elements[0]
        err = core.init_element_artifact(element, self.context)
        self.assertIsNone(err, err)
        artifact = element._get_artifact()
        key = artifact.get_extract_key()
        proto_path = os.path.join(
            self.context.artifactdir, element.get_artifact_name(key=key)
        )
        os.makedirs(os.path.dirname(proto_path), exist_ok=True)

        proto = artifact_pb2.Artifact()
        proto.version = 2
        proto.files.CopyFrom(tree_digest)
        # Real artifacts always carry these blobs; give them valid digests so
        # that casd's FindMissingBlobs does not choke on empty ones.
        proto.public_data.CopyFrom(self.writer.put(b"{}"))
        proto.low_diversity_meta.CopyFrom(self.writer.put(b"{}"))
        proto.high_diversity_meta.CopyFrom(self.writer.put(b"{}"))
        with open(proto_path, "wb") as f:
            f.write(proto.SerializeToString())
        return element

    def test_full_attribution_all_scope(self):
        self.fabricate("base.bst", self.base_tree)
        self.fabricate("lib.bst", self.lib_tree)
        self.fabricate("app.bst", self.app_tree)

        result = core.analyze_target(
            self.context, self.project, "app.bst", "all", None, None
        )
        self.assertNotIn("error", result, result.get("error"))
        self.assertEqual(result["dependencies"], ["base.bst", "lib.bst"])
        self.assertEqual(result["missing"], [])

        by_path = {e["path"]: e for e in result["entries"]}

        self.assertEqual(by_path["usr/bin/app"]["origin"], "own")
        self.assertEqual(by_path["usr/bin/app-stripped"]["origin"], "own")

        base_conf = by_path["usr/share/base.conf"]
        self.assertEqual(base_conf["origin"], "dependency")
        self.assertEqual(base_conf["matches"], [{"element": "base.bst", "path": "usr/share/base.conf", "exact": True}])

        libx = by_path["usr/lib/libx.so"]
        self.assertEqual(libx["origin"], "dependency")
        self.assertEqual(libx["matches"], [{"element": "lib.bst", "path": "usr/lib/libx.so", "exact": True}])

        renamed = by_path["opt/renamed-liby.so"]
        self.assertEqual(renamed["origin"], "moved")
        self.assertEqual(renamed["matches"], [{"element": "lib.bst", "path": "usr/lib/liby.so", "exact": False}])

        current = by_path["usr/share/current.conf"]
        self.assertEqual(current["origin"], "dependency")
        self.assertEqual(current["type"], "symlink")
        self.assertEqual(current["matches"], [{"element": "base.bst", "path": "usr/share/current.conf", "exact": True}])

        summary = result["summary"]
        self.assertEqual(summary["files"], 6)
        self.assertEqual(summary["from_dependency"], 3)
        self.assertEqual(summary["moved"], 1)
        self.assertEqual(summary["unattributed"], 2)

    def test_run_scope_empty_for_compose(self):
        self.fabricate("base.bst", self.base_tree)
        self.fabricate("lib.bst", self.lib_tree)
        self.fabricate("app.bst", self.app_tree)

        # compose elements take build-dependencies, so the runtime scope is
        # empty: no dependency is considered and nothing can be attributed.
        result = core.analyze_target(
            self.context, self.project, "app.bst", "run", None, None
        )
        self.assertEqual(result["dependencies"], [])

        by_path = {e["path"]: e for e in result["entries"]}
        self.assertEqual(by_path["usr/share/base.conf"]["origin"], "own")
        self.assertEqual(by_path["usr/lib/libx.so"]["origin"], "own")
        self.assertEqual(by_path["opt/renamed-liby.so"]["origin"], "own")

    def test_missing_artifact_reported(self):
        result = core.analyze_target(
            self.context, self.project, "app.bst", "all", None, None
        )
        self.assertIn("error", result)
        self.assertIn("not cached", result["error"])

    def test_junction_subproject_loads_without_fetch_callback_error(self):
        # Regression test: projects with junctions whose sources are not
        # cached require a fetch_subprojects callback on the loader's
        # LoadContext. Without it, load_elements() raises
        # TypeError: 'NoneType' object is not callable.
        project_dir = os.path.join(self.tmp, "junction-project")
        sub_dir = os.path.join(project_dir, "sub")
        os.makedirs(os.path.join(project_dir, "elements"))
        os.makedirs(os.path.join(project_dir, "files", "app"))
        os.makedirs(os.path.join(sub_dir, "elements"))

        with open(os.path.join(project_dir, "project.conf"), "w") as f:
            f.write("name: main\nmin-version: 2.0\nelement-path: elements\n")
        with open(os.path.join(project_dir, "elements", "sub.bst"), "w") as f:
            f.write("kind: junction\nsources:\n- kind: local\n  path: sub\n")
        with open(os.path.join(project_dir, "elements", "app.bst"), "w") as f:
            f.write(
                "kind: import\nsources:\n- kind: local\n  path: files/app\n"
                "depends:\n- sub.bst:tool.bst\n"
            )
        with open(os.path.join(project_dir, "files", "app", "hello.txt"), "w") as f:
            f.write("hello\n")
        with open(os.path.join(sub_dir, "project.conf"), "w") as f:
            f.write("name: sub\nmin-version: 2.0\nelement-path: elements\n")
        with open(os.path.join(sub_dir, "elements", "tool.bst"), "w") as f:
            f.write("kind: import\nsources:\n- kind: local\n  path: files/tool\n")
        os.makedirs(os.path.join(sub_dir, "files", "tool"))
        with open(os.path.join(sub_dir, "files", "tool", "tool.txt"), "w") as f:
            f.write("tool\n")

        project = core.load_project(self.context, project_dir)
        result = core.analyze_target(
            self.context, project, "app.bst", "all", None, None
        )
        # The elements must load (the junction gets fetched on demand); with
        # the regression the error is "failed to load element: 'NoneType'
        # object is not callable".
        error = result.get("error", "")
        self.assertNotIn("not callable", error)
        # Artifacts are not cached, which is reported cleanly:
        self.assertIn("not cached", error)
        # Subproject elements are reported with their junction prefix:
        self.assertEqual(
            [m["name"] for m in result["missing"]],
            ["sub.bst:tool.bst"],
        )

    def test_casd_failure_falls_back_to_filesystem(self):
        # Fabricate cached artifacts, then load fresh elements and simulate a
        # dead casd: Element._load_artifact() raises the exact error bst
        # raises when buildbox-casd dies. The tool must fall back to the
        # casd-free artifact resolution and still find the artifacts.
        self.fabricate("base.bst", self.base_tree)
        self.fabricate("lib.bst", self.lib_tree)
        self.fabricate("app.bst", self.app_tree)

        element = self.project.load_elements(["app.bst"])[0]
        with mock.patch.object(
            type(element),
            "_load_artifact",
            side_effect=CASCacheError(
                "buildbox-casd process died before connection could be established"
            ),
        ):
            err = core.init_element_artifact(element, self.context)
        self.assertIsNone(err, err)

        cas = self.context.get_cascache()
        digest, derr = core.artifact_files_digest(cas, element)
        self.assertIsNone(derr, derr)
        self.assertEqual(digest.hash, self.app_tree.hash)

    def test_cli_text_and_json(self):
        self.fabricate("base.bst", self.base_tree)
        self.fabricate("lib.bst", self.lib_tree)
        self.fabricate("app.bst", self.app_tree)

        text = io.StringIO()
        with contextlib.redirect_stdout(text):
            cli.main(["-C", EXAMPLE_DIR, "-d", "all", "app.bst"])
        text = text.getvalue()
        self.assertIn("usr/share/base.conf", text)
        self.assertIn("base.bst", text)
        self.assertIn("summary:", text)
        self.assertIn("lib.bst (as usr/lib/liby.so)", text)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["-C", EXAMPLE_DIR, "-d", "all", "-f", "json", "app.bst"])
        data = json.loads(buf.getvalue())
        self.assertIn("app.bst", data)
        self.assertEqual(data["app.bst"]["dependencies"], ["base.bst", "lib.bst"])
        self.assertIn("summary", data["app.bst"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
