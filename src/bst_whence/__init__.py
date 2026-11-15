# SPDX-License-Identifier: MIT
"""bst-whence — determine which dependency each file in a BuildStream 2
artifact came from.

See the README for usage, output format and limitations.
"""

import sys

__version__ = "0.1.1"

from .core import (  # noqa: E402,F401
    CasError,
    SCOPES,
    analyze_target,
    artifact_files_digest,
    attribute_files,
    directory_map,
    filter_entries,
    init_element_artifact,
    install_message_handler,
    iter_dependencies,
    load_project,
    read_directory,
    release_cas_resources,
    summarize,
)
from .report import render_json, render_text  # noqa: E402,F401

__all__ = [
    "CasError",
    "SCOPES",
    "__version__",
    "analyze_target",
    "artifact_files_digest",
    "attribute_files",
    "directory_map",
    "filter_entries",
    "init_element_artifact",
    "install_message_handler",
    "iter_dependencies",
    "load_project",
    "read_directory",
    "release_cas_resources",
    "render_json",
    "render_text",
    "summarize",
]


def entrypoint():
    """Console script entry point: run the CLI, degrading to a friendly
    message when BuildStream is not installed."""
    try:
        from .cli import main
    except ImportError as e:
        sys.exit("error: {}".format(e))
    main()
