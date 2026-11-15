# bst-whence

<!-- After creating the GitHub repository, uncomment and update the badge:
[![CI](https://github.com/<you>/bst-whence/actions/workflows/ci.yml/badge.svg)](https://github.com/<you>/bst-whence/actions/workflows/ci.yml)
-->

Determine which dependency each file in a **BuildStream 2** artifact came from.

```
$ bst-whence app.bst

target: app.bst (app.bst)
dependencies considered (build scope): 2
artifact files digest: a42b22128a61d9f6daa64ec9ea6bbf96fd243a183f1118ace12e78ef1e3dbfc8

provenance notes:
  (not from any dependency)   produced by this element's own build or sources
  X (as P)                    content identical to path `P` in dependency X
  [ambiguous: N]              identical content found in N dependencies

  opt/renamed-liby.so     ←  lib.bst (as usr/lib/liby.so)
  usr/bin/app             ←  (not from any dependency)
  usr/bin/app-stripped    ←  (not from any dependency)
  usr/lib/libx.so         ←  lib.bst
  usr/share/base.conf     ←  base.bst
  usr/share/current.conf  ←  base.bst

summary:
  files:                                   6
  from a dependency (same path):           3
  from a dependency (different path):      1
  ambiguous (several possible deps):       0
  not attributable to any dependency:      2
  files per dependency:
        2  base.bst
        2  lib.bst
```

## Requirements

* **Python ≥ 3.10**
* **BuildStream 2** — the tool imports the `buildstream` Python package, so run
  it inside the environment where `bst` works (venv, container, build host).
  Recent releases (≥ 2.8) bundle `buildbox-casd`; otherwise the binary must be
  on `PATH`, as for any `bst` invocation.
* The target and dependency **artifacts cached locally**. If they aren't, run
  `bst artifact pull <element>` first — the tool tells you exactly what's
  missing.

No builds, pushes or pulls are performed. Files are read directly from the
local CAS; the only casd interaction is the ordinary artifact cache query that
`bst` itself performs for every command. If casd is not running or fails to
start, bst-whence falls back to resolving artifacts directly from the local
filesystem (strict cache mode, which is BuildStream's default).

Projects with junctions are loaded exactly as `bst` loads them: if a
junction's sources are not in the source cache yet, they are fetched on
demand (which may require network access).

## Installation

Inside the environment that has BuildStream 2 installed:

```sh
pip install git+https://github.com/Herbstblatt/bst-whence.git
```

or from a checkout:

```sh
pip install .
```

This installs the `bst-whence` command (and `python -m bst_whence`). You can
also run it without installing:

```sh
python -m bst_whence        # with PYTHONPATH=src
```

## Usage

```
bst-whence [options] ELEMENT [ELEMENT...]

  -C, --directory DIR    directory containing project.conf (default: cwd)
  -c, --config FILE      BuildStream user configuration (default: discovery)
  -d, --deps S           build | run | all        (default: build)
  -f, --format F         text | json              (default: text)
  -o, --output FILE      write the report to FILE instead of stdout
  --by-dep               group text output by dependency instead of path
  --include GLOB         only report paths matching GLOB (repeatable)
  --exclude GLOB         skip paths matching GLOB (repeatable)
  --no-summary           omit the summary in text output
  --no-truncate          list every candidate for ambiguous matches
  -v, --verbose          show informational and status messages on stderr
  --debug                show debug messages on stderr (implies --verbose)
  --log-file FILE        also write log output to FILE
```

When a file's content is found in many dependencies (e.g. a license file
duplicated across a whole stack), the text report shows only as many
candidates as fit the terminal width — always at least one — and collapses
the rest into `… (+N more)`. Use `--no-truncate` to list them all. JSON
output is never truncated: it always contains the complete match lists.

## Logging

bst-whence logs to stderr in BuildStream's console style — elapsed and
wallclock timestamps, level labels, indented detail lines:

```
[00:00:03][17:14:18] INFO    base.bst: 2 files indexed
[00:00:03][17:14:18] STATUS  Reading artifact contents of app.bst
[00:00:03][17:14:18] INFO    Attribution: 3 from deps, 1 moved, 0 ambiguous, 2 unattributed
```

BuildStream's own messages (element loading, warnings, …) are forwarded
through its messenger and appear in the same format. Visibility follows the
usual convention:

* default: warnings and errors only
* `-v, --verbose`: additionally informational and status messages
* `--debug`: additionally debug messages and BuildStream's task delimiter
  events
* `--log-file FILE`: additionally write log output to FILE (informational
  and above, or debug and above with `--debug`), regardless of the console
  level

The report itself always goes to stdout (or `-o FILE`), so it stays clean to
pipe into other tools.

### Scope semantics (`--deps`)

* `build` (default) — the **build scope**: the element's build dependencies
  plus their runtime dependencies. This is exactly what is staged into the
  sandbox at build time, so it covers ordinary build elements as well as
  `compose` elements (which take build-dependencies in BuildStream 2).
* `run` — the runtime scope: the element's runtime dependencies. Use this for
  `stack`-like artifacts whose contents are the union of runtime
  dependencies.
* `all` — the full dependency graph.

### JSON output

`-f json` emits one object per target, keyed by target name, with the
per-file entries (`origin`: `dependency` | `moved` | `own`, `matches`,
`ambiguous`), the dependency list, missing artifacts and the summary:

```json
{
  "app.bst": {
    "element": "app.bst",
    "scope": "build",
    "artifact_digest": "a42b2212…",
    "dependencies": ["base.bst", "lib.bst"],
    "missing": [],
    "entries": [
      {"path": "usr/share/base.conf", "type": "file",
       "hash": "…", "size": 12, "origin": "dependency",
       "matches": [{"element": "base.bst", "path": "usr/share/base.conf", "exact": true}],
       "ambiguous": false}
    ],
    "summary": {"files": 6, "from_dependency": 3, "moved": 1,
                "ambiguous": 0, "unattributed": 2, "per_dependency": {"base.bst": 2, "lib.bst": 2}}
  }
}
```

## How it works

1. Load the project and the target element (the same loading path `bst`
   itself uses), then enumerate dependencies in the requested scope
   (`_Scope.BUILD` / `RUN` / `ALL`).
2. For each element, initialize its cache keys and artifact state
   (`Element._initialize_state` + `_load_artifact`, exactly like
   `Stream.query_cache`), then read its artifact metadata proto and extract
   the `files` directory digest.
3. Walk each artifact's directory tree by parsing REAPI `Directory` messages
   straight from the CAS (`<cachedir>/cas/objects/…`) — no materialization
   to disk. Each file is identified by its SHA-256 content digest, each
   symlink by its target. Shared subtrees are memoized by directory digest,
   which helps a lot since dependencies share large identical subtrees.
4. Attribute every target path:
   * same path + same digest in a dependency → `dependency` (copied from it);
   * same digest at a different path → `moved` (copied and renamed);
   * no digest match → `own` (produced by this element's build/sources).

## Limitations

* **Content identity, not tracing.** Files modified after being copied from a
  dependency (stripped binaries, rewritten configs) no longer match and are
  reported as `own`. There is no per-file provenance metadata in BuildStream
  artifacts, so digest comparison is the only post-hoc signal available.
* **Ambiguity.** If several dependencies contain byte-identical files at the
  same path (e.g. duplicated license files), all candidates are listed.
* **Cached artifacts required.** Dependencies whose artifacts are not in the
  local cache are listed under `missing` — their files simply cannot be
  attributed until pulled.
* Directories are structural and are not attributed.

## Troubleshooting

### `failed to load element: ... No package metadata was found for buildstream-plugins`

BuildStream loads external element plugins (`autotools`, `cmake`, `meson`,
…) from Python packages installed in the same environment, and the project
(junction) you're analyzing declares one of them. The error means
`bst-whence` is running in a Python environment that has `buildstream` but
not `buildstream-plugins` — usually because it's not the same environment
`bst` runs in (e.g. a different venv, or outside the container).

Check which environments are involved:

```sh
which bst-whence && head -1 "$(which bst-whence)"
which bst        && head -1 "$(which bst)"
```

Then either run `bst-whence` with the same interpreter as `bst`, or install
the plugins into the tool's environment:

```sh
python3 -m pip install buildstream-plugins
```

The installed `buildstream-plugins` version must be compatible with the
`buildstream` version in that environment (the project may also pin a
specific version; a mismatch surfaces as a *"Version conflict encountered
while loading plugin"* error instead).

### `error: artifact not cached locally`

The target (or a dependency's) artifact is not in the local cache. Run
`bst artifact pull <element>` in the same environment and retry.

## Library use

The analysis is exposed as a small library:

```python
import bst_whence
from buildstream._context import Context

context = Context()
context.load(config=None)
project = bst_whence.load_project(context, ".")
result = bst_whence.analyze_target(context, project, "app.bst", "build", None, None)
bst_whence.release_cas_resources(context)   # stop the casd monitor thread
```

## Development

```sh
pip install -e .
python -m unittest discover -s tests -v
```

* `tests/test_core.py` — CAS walking and attribution logic against a synthetic CAS.
* `tests/test_e2e.py` — full pipeline against a fabricated artifact cache for
  the `example/` project (artifact metadata protos + CAS blobs in bst's exact
  on-disk layout), so no builds are needed.

BuildStream's Python API used here is internal (`_`-prefixed) and not covered
by stability guarantees. The project is developed and tested against
BuildStream 2.8.x; the integration points it relies on (`Context`, `Project`,
`Element._initialize_state`/`_load_artifact`/`_dependencies`, `Artifact`, the
CAS on-disk layout) are the same ones `bst`'s own frontends use.

## License

MIT — see [LICENSE](LICENSE).
