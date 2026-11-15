# SPDX-License-Identifier: MIT
"""Core of bst-whence: CAS walking, BuildStream glue and provenance analysis.

BuildStream artifacts are content-addressed directory trees. When an element
is built, its build-scope dependencies are staged into the build sandbox, and
build scripts frequently copy files out of those staged dependencies into
their own install root. Because the copies are byte-for-byte identical, they
have the same content digest in the CAS. This module walks the artifact of a
target element and of every dependency, compares per-file content digests,
and attributes the provenance of every file:

  * "dependency": an identical file exists at the same relative path in one
    or more dependency artifacts (almost certainly copied from it).
  * "moved": an identical file exists in a dependency at a *different*
    relative path (copied and renamed during the build).
  * "own": no identical content in any dependency — produced by this
    element's own build, or by its own sources.

Limitations:

  * Files that are modified after being copied from a dependency (e.g.
    stripped binaries, rewritten paths) no longer match by content and are
    reported as "own". This module detects provenance by content identity,
    not by tracing.
  * If several dependencies contain byte-identical files at the same path
    (e.g. duplicated license files), all candidates are reported.
  * Directories are structural and are not attributed.
"""

import fnmatch
import os
import sys
from collections import defaultdict

from ._buildstream import Project, _Scope, remote_execution_pb2

SCOPES = {"build": _Scope.BUILD, "run": _Scope.RUN, "all": _Scope.ALL}


class CasError(Exception):
    """Raised when a directory tree cannot be fully read from the CAS."""


# ---------------------------------------------------------------------------
# CAS reading (pure filesystem access; no casd RPCs involved)
# ---------------------------------------------------------------------------

def read_directory(cas, digest):
    """Read and parse a REAPI Directory message from the CAS."""
    directory = remote_execution_pb2.Directory()
    try:
        with cas.open(digest, "rb") as f:
            directory.ParseFromString(f.read())
    except FileNotFoundError:
        raise CasError(
            "blob {} missing from the local CAS (pull the artifact with "
            "`bst artifact pull`)".format(digest.hash)
        )
    except Exception as e:
        raise CasError("failed to read blob {}: {}".format(digest.hash, e))
    return directory


def directory_map(cas, root_digest, memo=None):
    """Map every file and symlink in a directory tree to its content identity.

    Args:
        cas: CASCache-like object with an `open(digest, mode)` method.
        root_digest: Digest of the root Directory message.
        memo: Optional dict of directory digest hash -> {relpath: node} used
              to avoid re-parsing subtrees shared between artifacts.

    Returns:
        Dict mapping relative path (str, directory separator "/") to a node:
          ("file", sha256_hex, size_bytes)   for regular files
          ("symlink", target)                for symlinks
        Directories themselves are structural and not represented.

    Raises:
        CasError: if any blob in the tree is missing from the CAS.
    """
    if memo is None:
        memo = {}

    def rec(digest):
        key = digest.hash
        if key in memo:
            return memo[key]

        directory = read_directory(cas, digest)
        sub = {}
        for fnode in directory.files:
            sub[fnode.name] = ("file", fnode.digest.hash, fnode.digest.size_bytes)
        for snode in directory.symlinks:
            sub[snode.name] = ("symlink", snode.target)
        for dnode in directory.directories:
            child = rec(dnode.digest)
            for relpath, node in child.items():
                sub[dnode.name + "/" + relpath] = node

        memo[key] = sub
        return sub

    return rec(root_digest)


# ---------------------------------------------------------------------------
# BuildStream glue (element loading, artifact metadata, dependency enumeration)
# ---------------------------------------------------------------------------

def load_project(context, directory):
    install_message_handler(context)
    project = Project(directory, context)
    project.ensure_fully_loaded()
    return project


def init_element_artifact(element, context):
    """Initialize the element's cache keys and Artifact object.

    Replicates what BuildStream's own frontends do (`Stream.query_cache` →
    `Element._load_artifact`): compute cache keys, query the local artifact
    cache and attach the resulting Artifact object to the element.

    Returns an error string, or None on success.
    """
    try:
        element._initialize_state()
    except Exception as e:
        return "failed to initialize element state: {}".format(e)

    try:
        element._load_artifact(pull=False)
        element._load_artifact_done()
    except Exception as e:
        return "failed to query the artifact cache: {}".format(e)
    return None


def release_cas_resources(context):
    """Stop background threads started by the CASCache (e.g. the casd usage
    monitor), so the process can exit cleanly."""
    if context._cascache is not None:
        try:
            context._cascache.release_resources()
        except Exception:
            pass


def install_message_handler(context):
    """Install a minimal message handler so BuildStream internals (which
    assert that one exists) don't crash; forward warnings to stderr."""
    def handler(message, is_silenced=False):
        try:
            message_type = message.message_type.name
        except Exception:
            message_type = "UNKNOWN"
        if message_type in ("WARN", "ERROR") and not is_silenced:
            try:
                brief = message.message
            except Exception:
                brief = str(message)
            sys.stderr.write("{}: {}\n".format(message_type, brief))

    context.messenger.set_message_handler(handler)


def iter_dependencies(element, scope_name):
    """Yield the element's dependencies in the requested scope, deduplicated
    and sorted by name. The element itself is never included."""
    scope = SCOPES[scope_name]
    seen = set()
    deps = []
    for dep in element._dependencies(scope, recurse=True):
        if dep._unique_id == element._unique_id:
            continue
        if dep._unique_id in seen:
            continue
        seen.add(dep._unique_id)
        deps.append(dep)
    deps.sort(key=lambda e: e.name)
    return deps


def artifact_files_digest(cas, element):
    """Return (digest, error) for the element's artifact `files` directory.

    Reads the artifact metadata from the local artifact cache only; no
    casd calls are involved."""
    artifact = element._get_artifact()

    try:
        proto = artifact._load_proto()
    except Exception as e:
        return None, "failed to read artifact metadata: {}".format(e)
    if proto is None:
        return None, "artifact not cached locally"

    digest = proto.files
    if digest is None or not str(digest):
        return None, "artifact contains no files"

    if not os.path.exists(cas.objpath(digest)):
        return None, "artifact cached but file blobs are missing locally"

    return digest, None


# ---------------------------------------------------------------------------
# Provenance analysis
# ---------------------------------------------------------------------------

def attribute_files(target_map, dep_maps, dep_names):
    """Attribute every path in `target_map` to the dependencies.

    Returns (entries, per_dependency_counts) where each entry is a dict:
      path, type ("file"|"symlink"), hash, size, target,
      origin ("dependency"|"moved"|"own"),
      matches ([{element, path, exact}]), ambiguous (bool)
    """
    digest_index = defaultdict(list)  # sha256 -> [(dep_name, dep_path)]
    for name in dep_names:
        for path, (kind, *rest) in dep_maps[name].items():
            if kind == "file":
                digest_index[rest[0]].append((name, path))

    entries = []
    per_dep = defaultdict(int)

    for path, node in sorted(target_map.items()):
        kind = node[0]
        exact = [dn for dn in dep_names if dep_maps[dn].get(path) == node]

        matches = []
        origin = "own"
        if exact:
            origin = "dependency"
            for dn in exact:
                matches.append({"element": dn, "path": path, "exact": True})
                per_dep[dn] += 1
        elif kind == "file" and node[1] in digest_index:
            origin = "moved"
            for dn, srcpath in digest_index[node[1]]:
                matches.append({"element": dn, "path": srcpath, "exact": False})
                per_dep[dn] += 1

        entry = {
            "path": path,
            "type": "symlink" if kind == "symlink" else "file",
            "hash": node[1] if kind == "file" else None,
            "size": node[2] if kind == "file" else None,
            "target": node[1] if kind == "symlink" else None,
            "origin": origin,
            "matches": matches,
            "ambiguous": len(matches) > 1,
        }
        entries.append(entry)

    return entries, dict(per_dep)


def summarize(entries, per_dep):
    from_dep = sum(1 for e in entries if e["origin"] == "dependency")
    moved = sum(1 for e in entries if e["origin"] == "moved")
    ambiguous = sum(1 for e in entries if e["ambiguous"])
    return {
        "files": len(entries),
        "from_dependency": from_dep,
        "moved": moved,
        "ambiguous": ambiguous,
        "unattributed": len(entries) - from_dep - moved,
        "per_dependency": dict(sorted(per_dep.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def filter_entries(entries, include, exclude):
    kept = []
    for entry in entries:
        path = entry["path"]
        if include and not any(fnmatch.fnmatch(path, pat) for pat in include):
            continue
        if exclude and any(fnmatch.fnmatch(path, pat) for pat in exclude):
            continue
        kept.append(entry)
    return kept


# ---------------------------------------------------------------------------
# Per-target analysis
# ---------------------------------------------------------------------------

def analyze_target(context, project, target_name, scope_name, include, exclude):
    result = {
        "target": target_name,
        "scope": scope_name,
        "dependencies": [],
        "missing": [],
    }

    try:
        elements = project.load_elements([target_name])
        element = elements[0]
    except Exception as e:
        result["error"] = "failed to load element: {}".format(e)
        return result

    result["element"] = element.name

    cas = context.get_cascache()
    memo = {}

    # Initialize dependencies first (staging order), then the target: cache
    # keys can only be computed once the keys of dependencies are known.
    dep_names = []
    dep_maps = {}
    for dep in iter_dependencies(element, scope_name):
        err = init_element_artifact(dep, context)
        if err:
            result["missing"].append({"name": dep.name, "reason": err})
            continue
        dep_digest, derr = artifact_files_digest(cas, dep)
        if derr:
            result["missing"].append({"name": dep.name, "reason": derr})
            continue
        try:
            dep_maps[dep.name] = directory_map(cas, dep_digest, memo)
        except CasError as e:
            result["missing"].append({"name": dep.name, "reason": str(e)})
            continue
        dep_names.append(dep.name)

    err = init_element_artifact(element, context)
    if err:
        result["error"] = err
        return result

    target_digest, err = artifact_files_digest(cas, element)
    if err:
        result["error"] = err
        return result

    try:
        target_map = directory_map(cas, target_digest, memo)
    except CasError as e:
        result["error"] = "artifact file blobs are incomplete in the local CAS: {}".format(e)
        return result

    result["artifact_digest"] = target_digest.hash

    entries, per_dep = attribute_files(target_map, dep_maps, dep_names)
    if include or exclude:
        entries = filter_entries(entries, include, exclude)
        # Recompute per-dep counts from the filtered entries only.
        filtered_per_dep = defaultdict(int)
        for entry in entries:
            for m in entry["matches"]:
                filtered_per_dep[m["element"]] += 1
        per_dep = dict(filtered_per_dep)

    result["dependencies"] = dep_names
    result["entries"] = entries
    result["summary"] = summarize(entries, per_dep)
    return result
