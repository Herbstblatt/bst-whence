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

import datetime
import fnmatch
import os
import re
import sys
import traceback
from collections import defaultdict

from ._buildstream import (
    Artifact,
    MessageType,
    Project,
    Stream,
    _Scope,
    remote_execution_pb2,
    unconditional_messages,
)
from .log import get_logger

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
    logger = get_logger()
    logger.status("Loading project from {}".format(directory))
    install_message_handler(context)

    # Create a Stream exactly like the bst frontend does. While loading
    # elements, the loader needs a `fetch_subprojects` callback to fetch
    # junction sources that are not cached yet; Stream provides it. The
    # scheduler unconditionally invokes its ticker/interrupt callbacks, so
    # provide no-ops (bst's frontend passes its UI callbacks here).
    stream = Stream(
        context,
        datetime.datetime.now(),
        ticker_callback=lambda: None,
        interrupt_callback=lambda: None,
    )
    stream.init()
    try:
        project = Project(directory, context, fetch_subprojects=stream.fetch_subprojects)
    except TypeError:
        # Older BuildStream versions do not accept fetch_subprojects here
        # and wire it up in Stream.set_project() instead.
        project = Project(directory, context)
    stream.set_project(project)
    if project.load_context.fetch_subprojects is None:
        project.load_context.set_fetch_subprojects(stream.fetch_subprojects)
    context._bst_whence_stream = stream

    project.ensure_fully_loaded()
    logger.debug("Project loaded: {}".format(project.name))
    return project


def _looks_like_casd_error(error):
    """Heuristic for whether an exception comes from casd being unavailable
    (dead process, broken RPC channel, ...) rather than from a genuine
    problem with the element or the artifact metadata."""
    cls = type(error)
    if cls.__name__ == "CASCacheError":
        return True
    module = cls.__module__ or ""
    if module.startswith("grpc") or "RpcError" in cls.__name__:
        return True
    return False


def _init_artifact_direct(element, context):
    """Construct the element's Artifact object without any casd interaction.

    This mirrors the strict branch of `Element._load_artifact()`, minus the
    casd-backed `query_cache()` call. Only valid when the project uses strict
    cache mode, where the strong cache key equals the strict cache key.

    Returns an error string, or None on success.
    """
    strict_key = element._Element__strict_cache_key
    if strict_key is None:
        return "cache keys unavailable (a build dependency has no cache key yet)"
    artifact = Artifact(
        element,
        context,
        strict_key=strict_key,
        strong_key=strict_key,
        weak_key=element._Element__weak_cache_key,
    )
    element._Element__artifact = artifact
    return None


def init_element_artifact(element, context):
    """Initialize the element's cache keys and Artifact object.

    Replicates what BuildStream's own frontends do (`Stream.query_cache` →
    `Element._load_artifact`): compute cache keys, query the local artifact
    cache and attach the resulting Artifact object to the element.

    If casd is unavailable or dies, falls back to constructing the Artifact
    directly (strict cache mode only): everything bst-whence actually needs
    from the artifact cache is on the local filesystem.

    Returns an error string, or None on success.
    """
    try:
        element._initialize_state()
    except Exception as e:
        return "failed to initialize element state: {}".format(e)

    try:
        element._load_artifact(pull=False)
        element._load_artifact_done()
        return None
    except Exception as e:
        if not _looks_like_casd_error(e) or not context.get_strict():
            return "failed to query the artifact cache: {}".format(e)
        err = _init_artifact_direct(element, context)
        if err:
            return "failed to query the artifact cache: {} ({})".format(e, err)
        get_logger().warn_once(
            "casd-fallback",
            "casd is not responding ({}); falling back to filesystem-only "
            "artifact resolution".format(e),
        )
        return None


def release_cas_resources(context):
    """Stop background threads started by the CASCache (e.g. the casd usage
    monitor), so the process can exit cleanly. Also cleans up the Stream used
    for project loading."""
    if context._cascache is not None:
        try:
            context._cascache.release_resources()
            get_logger().debug("Released CAS resources")
        except Exception:
            pass
    stream = getattr(context, "_bst_whence_stream", None)
    if stream is not None:
        try:
            stream.cleanup()
        except Exception:
            pass
        try:
            # Ensure no scheduler worker threads are left behind if a fetch
            # failed partway through.
            stream._scheduler.terminate()
        except Exception:
            pass


def install_message_handler(context):
    """Install a message handler that forwards BuildStream's own messages
    into bst-whence's logger, so they appear as bst-style console lines on
    stderr (visibility follows the logger's verbosity)."""
    logger = get_logger()

    def handler(message, is_silenced=False):
        message_type = getattr(message, "message_type", None)
        if is_silenced and message_type not in unconditional_messages:
            return
        text = getattr(message, "message", str(message))
        detail = getattr(message, "detail", None)

        if message_type == MessageType.DEBUG:
            logger.debug(text, detail)
        elif message_type == MessageType.STATUS:
            logger.status(text, detail)
        elif message_type in (MessageType.START, MessageType.SUCCESS, MessageType.FAIL, MessageType.SKIPPED):
            # Task delimiter events; they repeat the task text, so keep them
            # for --debug only.
            logger.debug(text, detail)
        elif message_type == MessageType.INFO:
            logger.info(text, detail)
        elif message_type == MessageType.WARN:
            logger.warning(text, detail)
        elif message_type in (MessageType.ERROR, MessageType.BUG):
            logger.error(text, detail)
        else:  # MessageType.LOG and anything unexpected
            logger.debug(text, detail)

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


_PACKAGE_NOT_FOUND_RE = re.compile(r"No package metadata was found for '?([A-Za-z0-9_.-]+)'?")


def _hint_for_load_error(error):
    """Return a human hint for common project-loading failures, or None.

    In particular, BuildStream loads external element plugins (autotools,
    cmake, meson, ...) from Python packages such as `buildstream-plugins`
    installed in the same environment; when that package is missing the
    error is cryptic, so translate it into an actionable hint."""
    text = str(error)
    match = _PACKAGE_NOT_FOUND_RE.search(text)
    if match:
        package = match.group(1)
        return (
            "hint: the Python package '{}' is not installed in the environment "
            "where bst-whence is running (BuildStream loads element plugins "
            "from installed Python packages). bst-whence is running under the "
            "interpreter {}. Install it there — e.g. `{} -m pip install {}` — "
            "or run bst-whence with the same Python environment as `bst`."
        ).format(package, sys.executable, sys.executable, package)
    if "Failed to load element plugin" in text:
        return (
            "hint: external element plugins (autotools, cmake, meson, ...) ship "
            "in the 'buildstream-plugins' Python package. Ensure it is installed "
            "in the same Python environment as bst-whence "
            "(`pip install buildstream-plugins`)."
        )
    return None


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
    logger = get_logger()
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
        logger.debug("Traceback for failed element load:", detail=traceback.format_exc())
        result["error"] = "failed to load element: {}".format(e)
        hint = _hint_for_load_error(e)
        if hint:
            result["error"] += "\n" + hint
        else:
            result["error"] += "\nhint: rerun with --debug to see the full traceback"
        return result

    result["element"] = element.name
    logger.status("Analyzing {} ({} scope)".format(element.name, scope_name))

    cas = context.get_cascache()
    memo = {}

    # Initialize dependencies first (staging order), then the target: cache
    # keys can only be computed once the keys of dependencies are known.
    dep_names = []
    dep_maps = {}
    for dep in iter_dependencies(element, scope_name):
        err = init_element_artifact(dep, context)
        if err:
            logger.warning("{}: {}".format(dep.name, err))
            result["missing"].append({"name": dep.name, "reason": err})
            continue
        dep_digest, derr = artifact_files_digest(cas, dep)
        if derr:
            logger.warning("{}: {}".format(dep.name, derr))
            result["missing"].append({"name": dep.name, "reason": derr})
            continue
        try:
            with logger.timed_activity("Reading artifact contents of {}".format(dep.name)):
                dep_maps[dep.name] = directory_map(cas, dep_digest, memo)
        except CasError as e:
            logger.warning("{}: {}".format(dep.name, e))
            result["missing"].append({"name": dep.name, "reason": str(e)})
            continue
        logger.info("{}: {} files indexed".format(dep.name, len(dep_maps[dep.name])))
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
        with logger.timed_activity("Reading artifact contents of {}".format(element.name)):
            target_map = directory_map(cas, target_digest, memo)
    except CasError as e:
        result["error"] = "artifact file blobs are incomplete in the local CAS: {}".format(e)
        return result

    result["artifact_digest"] = target_digest.hash
    logger.info("{}: {} files indexed".format(element.name, len(target_map)))

    entries, per_dep = attribute_files(target_map, dep_maps, dep_names)
    if include or exclude:
        entries = filter_entries(entries, include, exclude)
        # Recompute per-dep counts from the filtered entries only.
        filtered_per_dep = defaultdict(int)
        for entry in entries:
            for m in entry["matches"]:
                filtered_per_dep[m["element"]] += 1
        per_dep = dict(filtered_per_dep)

    summary = summarize(entries, per_dep)
    logger.info(
        "Attribution: {} from deps, {} moved, {} ambiguous, {} unattributed".format(
            summary["from_dependency"],
            summary["moved"],
            summary["ambiguous"],
            summary["unattributed"],
        )
    )

    result["dependencies"] = dep_names
    result["entries"] = entries
    result["summary"] = summary
    return result
