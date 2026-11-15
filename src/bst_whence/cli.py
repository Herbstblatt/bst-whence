# SPDX-License-Identifier: MIT
"""Command line interface for bst-whence."""

import argparse
import io
import os
import sys

from . import __version__
from ._buildstream import Context
from .core import (
    SCOPES,
    analyze_target,
    install_message_handler,
    load_project,
    release_cas_resources,
)
from .log import get_logger
from .report import render_json, render_text


def build_parser():
    parser = argparse.ArgumentParser(
        prog="bst-whence",
        description=(
            "Determine which dependency each file in a BuildStream 2 artifact "
            "came from, by comparing content digests across the local artifact "
            "cache (CAS)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  bst-whence app.bst\n"
            "  bst-whence -d all -f json app.bst > provenance.json\n"
            "  bst-whence --by-dep --include 'usr/share/*' app.bst\n"
        ),
    )
    parser.add_argument(
        "targets", nargs="+", metavar="ELEMENT",
        help="target element(s), as project-relative .bst paths",
    )
    parser.add_argument(
        "-C", "--directory", default=None, metavar="DIR",
        help="directory containing project.conf (default: current directory)",
    )
    parser.add_argument(
        "-c", "--config", default=None, metavar="FILE",
        help="BuildStream user configuration file (default: standard discovery)",
    )
    parser.add_argument(
        "-d", "--deps", choices=sorted(SCOPES), default="build",
        help=(
            "which dependencies to consider. 'build' (default): the "
            "dependencies staged into the sandbox at build time (build "
            "dependencies and their runtime dependencies — covers ordinary "
            "build elements and compose elements); 'run': runtime "
            "dependencies only (for stack-like artifacts); 'all': the full "
            "dependency graph"
        ),
    )
    parser.add_argument(
        "-f", "--format", dest="format_", choices=("text", "json"), default="text",
        help="output format (default: text)",
    )
    parser.add_argument(
        "-o", "--output", default=None, metavar="FILE",
        help="write the report to FILE instead of stdout",
    )
    parser.add_argument(
        "--by-dep", action="store_true",
        help="group the text output by dependency instead of by path",
    )
    parser.add_argument(
        "--include", action="append", default=None, metavar="GLOB",
        help="only report artifact paths matching GLOB (may be repeated)",
    )
    parser.add_argument(
        "--exclude", action="append", default=None, metavar="GLOB",
        help="exclude artifact paths matching GLOB (may be repeated)",
    )
    parser.add_argument(
        "--no-summary", action="store_true", help="omit the summary in text output",
    )
    parser.add_argument(
        "--no-truncate", action="store_true",
        help="list every candidate for ambiguous matches, even if the lines "
             "do not fit the terminal (by default only as many candidates as "
             "fit are shown)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="show informational and status messages on stderr",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="show debug messages on stderr (implies --verbose)",
    )
    parser.add_argument(
        "--log-file", default=None, metavar="FILE",
        help="additionally write log messages to FILE (informational and "
             "above, or debug and above with --debug)",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    logger = get_logger()
    logger.configure(verbose=args.verbose, debug=args.debug, log_file=args.log_file)
    logger.debug("bst-whence {}".format(__version__))

    if args.directory:
        os.chdir(args.directory)

    try:
        context = Context()
        context.load(config=args.config)
    except Exception as e:
        sys.exit("error: failed to load BuildStream configuration: {}".format(e))

    install_message_handler(context)

    try:
        project = load_project(context, os.getcwd())
    except Exception as e:
        sys.exit("error: failed to load the BuildStream project: {}".format(e))

    results = []
    try:
        for target in args.targets:
            logger.status("Analyzing target {}".format(target))
            try:
                result = analyze_target(
                    context, project, target, args.deps, args.include, args.exclude
                )
            except Exception as e:
                result = {"target": target, "scope": args.deps, "error": str(e)}
            results.append(result)

        if args.format_ == "json":
            report = render_json(results)
        else:
            buf = io.StringIO()
            render_text(
                results, buf, args.by_dep,
                show_summary=not args.no_summary,
                truncate=not args.no_truncate,
            )
            report = buf.getvalue()

        if args.output:
            with open(args.output, "w") as f:
                f.write(report)
                if not report.endswith("\n"):
                    f.write("\n")
            logger.info("Report written to {}".format(args.output))
        else:
            sys.stdout.write(report)
            if not report.endswith("\n"):
                sys.stdout.write("\n")
    finally:
        release_cas_resources(context)
        logger.close()


if __name__ == "__main__":
    main()
