#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
#
# Core tests for bst-whence: exercise the CAS walking and provenance
# attribution logic against a synthetic CAS, without needing bst or casd.
#
# Run with the BuildStream 2 Python environment available (for the REAPI
# protobuf classes), e.g.:
#   <venv>/bin/python tests/test_core.py

import hashlib
import os
import shutil
import tempfile
import unittest

import bst_whence.core as core

from buildstream._protos.build.bazel.remote.execution.v2 import remote_execution_pb2


class FakeCas:
    """A minimal CAS storing blobs in the same on-disk layout as BuildStream:
    <root>/objects/<hash[:2]>/<hash[2:]>."""

    def __init__(self, root):
        self.root = root
        os.makedirs(os.path.join(root, "objects"), exist_ok=True)

    def objpath(self, digest):
        return os.path.join(self.root, "objects", digest.hash[:2], digest.hash[2:])

    def open(self, digest, mode="r"):
        return open(self.objpath(digest), mode)

    def put(self, data):
        """Store a blob, return its Digest."""
        digest = remote_execution_pb2.Digest()
        digest.hash = hashlib.sha256(data).hexdigest()
        digest.size_bytes = len(data)
        path = self.objpath(digest)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return digest

    def make_directory(self, files=(), symlinks=(), directories=()):
        """Build a Directory message in the CAS, return its Digest.

        files:        [(name, content, is_executable)]
        symlinks:     [(name, target)]
        directories:  [(name, digest)]
        """
        directory = remote_execution_pb2.Directory()
        for name, content, executable in files:
            fnode = directory.files.add()
            fnode.name = name
            fnode.digest.CopyFrom(self.put(content))
            fnode.is_executable = executable
        for name, target in symlinks:
            snode = directory.symlinks.add()
            snode.name = name
            snode.target = target
        for name, digest in directories:
            dnode = directory.directories.add()
            dnode.name = name
            dnode.digest.CopyFrom(digest)
        serialized = directory.SerializeToString()
        return self.put(serialized)


class TestDirectoryMap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cas = FakeCas(os.path.join(self.tmp, "cas"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def build_tree(self):
        subdir = self.cas.make_directory(
            files=[("deep.txt", b"deep-content", False)]
        )
        root = self.cas.make_directory(
            files=[
                ("hello.txt", b"hello world", False),
                ("run.sh", b"#!/bin/sh\n", True),
                ("empty.bin", b"", False),
            ],
            symlinks=[("latest.txt", "hello.txt")],
            directories=[("sub", subdir)],
        )
        return root

    def test_walk(self):
        root = self.build_tree()
        m = core.directory_map(self.cas, root)
        self.assertEqual(
            m,
            {
                "hello.txt": ("file", hashlib.sha256(b"hello world").hexdigest(), 11),
                "run.sh": ("file", hashlib.sha256(b"#!/bin/sh\n").hexdigest(), 10),
                "empty.bin": ("file", hashlib.sha256(b"").hexdigest(), 0),
                "latest.txt": ("symlink", "hello.txt"),
                "sub/deep.txt": ("file", hashlib.sha256(b"deep-content").hexdigest(), 12),
            },
        )

    def test_walk_memo_shares_subtrees(self):
        root = self.build_tree()
        memo = {}
        m1 = core.directory_map(self.cas, root, memo)
        m2 = core.directory_map(self.cas, root, memo)
        self.assertEqual(m1, m2)
        # The memo must have entries for the root and the subdirectory.
        self.assertGreaterEqual(len(memo), 2)

    def test_walk_missing_blob_raises(self):
        digest = remote_execution_pb2.Digest()
        digest.hash = "0" * 64
        digest.size_bytes = 1
        with self.assertRaises(core.CasError):
            core.directory_map(self.cas, digest)


class TestAttribute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cas = FakeCas(os.path.join(self.tmp, "cas"))
        self.addCleanup(shutil.rmtree, self.tmp)

        # dep1: provides a config file, a library and a symlink.
        self.dep1 = self.cas.make_directory(
            files=[
                ("usr/share/base.conf", b"base-config", False),
                ("usr/lib/libx.so", b"BINARYX", False),
            ],
            symlinks=[("usr/share/current.conf", "base.conf")],
        )
        # dep2: contains an identical copy of libx.so (ambiguity test) and
        # its own file.
        self.dep2 = self.cas.make_directory(
            files=[
                ("usr/lib/libx.so", b"BINARYX", False),
                ("usr/lib/liby.so", b"BINARYY", False),
            ]
        )
        # target: a mix of own files, copied files, a renamed copy, a
        # matching symlink, an own symlink, and a modified file.
        self.target = self.cas.make_directory(
            files=[
                ("usr/bin/app", b"APP", False),                       # own
                ("usr/share/base.conf", b"base-config", False),       # from dep1
                ("usr/lib/libx.so", b"BINARYX", False),               # ambiguous dep1/dep2
                ("usr/lib/liby.so", b"BINARYY", False),               # from dep2
                ("opt/renamed-liby.so", b"BINARYY", False),           # moved from dep2
                ("usr/bin/app-stripped", b"APP-modified", False),     # own (modified)
            ],
            symlinks=[
                ("usr/share/current.conf", "base.conf"),              # from dep1
                ("usr/share/other.conf", "nowhere"),                  # own
            ],
        )

        self.dep_names = ["dep1.bst", "dep2.bst"]
        self.dep_maps = {
            "dep1.bst": core.directory_map(self.cas, self.dep1),
            "dep2.bst": core.directory_map(self.cas, self.dep2),
        }
        self.target_map = core.directory_map(self.cas, self.target)

    def test_attribution(self):
        entries, per_dep = core.attribute_files(
            self.target_map, self.dep_maps, self.dep_names
        )
        by_path = {e["path"]: e for e in entries}

        # Own output.
        self.assertEqual(by_path["usr/bin/app"]["origin"], "own")
        self.assertEqual(by_path["usr/bin/app"]["matches"], [])
        # Modified copy of a dependency file is not falsely attributed.
        self.assertEqual(by_path["usr/bin/app-stripped"]["origin"], "own")

        # Exact copy from dep1.
        self.assertEqual(by_path["usr/share/base.conf"]["origin"], "dependency")
        self.assertEqual(
            by_path["usr/share/base.conf"]["matches"],
            [{"element": "dep1.bst", "path": "usr/share/base.conf", "exact": True}],
        )
        self.assertFalse(by_path["usr/share/base.conf"]["ambiguous"])

        # Identical content in both deps -> ambiguous.
        self.assertEqual(by_path["usr/lib/libx.so"]["origin"], "dependency")
        self.assertTrue(by_path["usr/lib/libx.so"]["ambiguous"])
        self.assertEqual(
            {m["element"] for m in by_path["usr/lib/libx.so"]["matches"]},
            {"dep1.bst", "dep2.bst"},
        )

        # Moved/renamed copy: content matches dep2 at a different path.
        self.assertEqual(by_path["opt/renamed-liby.so"]["origin"], "moved")
        self.assertEqual(
            by_path["opt/renamed-liby.so"]["matches"],
            [{"element": "dep2.bst", "path": "usr/lib/liby.so", "exact": False}],
        )

        # Symlinks match by target.
        self.assertEqual(by_path["usr/share/current.conf"]["origin"], "dependency")
        self.assertEqual(by_path["usr/share/other.conf"]["origin"], "own")

        # Per-dependency counts.
        self.assertEqual(per_dep["dep1.bst"], 3)  # base.conf, libx.so, current.conf
        self.assertEqual(per_dep["dep2.bst"], 3)  # libx.so, liby.so, renamed-liby.so

        # Summary.
        summary = core.summarize(entries, per_dep)
        self.assertEqual(summary["files"], 8)
        self.assertEqual(summary["from_dependency"], 4)
        self.assertEqual(summary["moved"], 1)
        self.assertEqual(summary["ambiguous"], 1)
        self.assertEqual(summary["unattributed"], 3)

    def test_filter(self):
        entries, _ = core.attribute_files(
            self.target_map, self.dep_maps, self.dep_names
        )
        kept = core.filter_entries(entries, ["usr/share/*"], None)
        self.assertEqual(
            {e["path"] for e in kept},
            {"usr/share/base.conf", "usr/share/current.conf", "usr/share/other.conf"},
        )
        kept = core.filter_entries(entries, None, ["*liby*", "*other*"])
        self.assertNotIn("usr/lib/liby.so", {e["path"] for e in kept})
        self.assertNotIn("usr/share/other.conf", {e["path"] for e in kept})


if __name__ == "__main__":
    unittest.main(verbosity=2)
