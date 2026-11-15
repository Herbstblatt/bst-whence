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
`bst` itself performs for every command.

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
```

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
