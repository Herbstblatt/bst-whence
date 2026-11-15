# SPDX-License-Identifier: MIT
"""Guarded imports of the BuildStream 2 Python API.

Everything bst-whence uses from BuildStream lives here, so that the rest of
the package imports cleanly and callers get a single, descriptive ImportError
when BuildStream is not installed.
"""

try:
    from buildstream._context import Context
    from buildstream._project import Project
    from buildstream._artifact import Artifact
    from buildstream._message import MessageType, unconditional_messages
    from buildstream._stream import Stream
    from buildstream.types import _Scope
    from buildstream._protos.build.bazel.remote.execution.v2 import remote_execution_pb2
except ImportError as e:
    raise ImportError(
        "bst-whence requires the BuildStream 2 Python API, which is not "
        "importable in this environment: {}".format(e)
    ) from e

__all__ = [
    "Artifact",
    "Context",
    "MessageType",
    "Project",
    "Stream",
    "_Scope",
    "remote_execution_pb2",
    "unconditional_messages",
]
