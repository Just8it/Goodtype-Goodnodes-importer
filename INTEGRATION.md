# Release contract

The desktop download/import menu is a separate Goodtype change. This repository supplies a
standalone Windows x64 release; it adds no runtime dependency to Goodtype itself.

## Package

`goodtype-goodnotes-importer-<version>-windows-x64.zip` contains one folder:

```text
goodtype-goodnotes-import.exe
decode.py
runtime/                  # private CPython and pinned decoder packages
importer-manifest.json    # component versions, source revisions and file hashes
licenses/                 # Rust dependency licenses
LICENSE
README.md
THIRD_PARTY.md
```

The sibling release JSON describes the target, contract/schema versions, archive URL, size and
SHA-256. `SHA256SUMS` covers the ZIP and JSON. A checksum downloaded alongside an untrusted
archive is not an independent trust anchor: Goodtype should pin the tested archive hash in its
own release, or verify a signed manifest using an app-pinned key.

The executable resolves `decode.py` and `runtime/` beside itself. Only debug builds may use the
source checkout's `.venv`; release builds never fall back to a build-machine or PATH interpreter.
No user-side package installation or runtime network operation occurs.

## Compatibility and execution

`goodtype-goodnotes-import.exe --version --json` returns:

```json
{"name":"goodtype-goodnotes-import","version":"0.1.1","protocolVersion":1,"notebookSchemaVersion":3,"decoderCommit":"66028455fd7fc61b7931679915d811a25485e3ad"}
```

`--check` runs the pinned decoder check under the existing process limits and prints its Python,
decoder and pypdf versions. Exit 0 means success; any failure exits 1. Imports also exit 1 on
partial batch failure while preserving completed notebooks. A new destination's
`import-report.json` starts with `pending` entries and records `completed`/`failed`, source,
target, page count and any error. Each completed notebook has a detailed page report and its
original export. Human-readable stdout progress is not a stable machine event stream.

Version 0.1.1 maps supported source shapes to canonical editable lines, rectangles, ellipses and
splines. Cubic controls are retained; quadratic segments convert exactly to cubic handles.
Rotated ellipses keep their centre and convert source radians to canonical degrees. Unsupported
styles/geometry stay explicit artwork: custom dash patterns, arrows, translucent fills, multiple
subpaths and paths beyond the 256-knot limit are never silently simplified. Some arrow SVGs
remain rejected by the existing strict SVG boundary; this release does not claim arrow support.

Each fallback SVG has its own cropped viewBox and matching canonical x/y/width/height, including
stroke/curve bounds and image cropping/rotation. Compatible marks within 12 points may merge,
up to 180 points per axis and 128 source objects per group. A single large source object stays
large. Marks may cross intervening artwork only when their padded bounds are disjoint; native
ink/shapes and ordinary images are ordering barriers. Cropped images remain separate. Grouping
therefore preserves the existing decoded paint order and does not join an entire page through
a chain of nearby marks. Bounds are conservative, so some whitespace around curves is retained.

Per-page notebook reports add `editable_shapes`; each `derived` entry includes its placement and
`source_count`. Rust verifies that native objects plus these counts account for every decoded
source object. Internal disposable decoder JSON is version 2 and must come from the same bundle;
the public CLI protocol remains 1 and canonical notebook schema remains 3. Existing imported
notebooks are unchanged; these improvements apply to new conversions into fresh folders.

For a future GUI, Rust should own the selected input/destination paths and the verified fixed
executable, run it in the background, read the bounded result report, and validate the generated
notebooks through the app's current core before publication into the selected Library. The
frontend must not choose arbitrary executables or shell arguments. Process termination cancels
conversion; the existing manifest-last publication preserves completed notebooks. A cancelled
job may leave an incomplete directory without a notebook manifest. Retry in a fresh batch folder.

Installation is optional, explicit on first use and cached for offline use. Download through one
Rust-owned HTTPS path to the fixed GitHub release and necessary asset redirect hosts, enforce
size/path/link limits, verify integrity, then atomically activate a new version directory. Keep
the prior installation on failure. No GitHub/Goodnotes account or notebook upload is needed.
No arbitrary latest-version download at startup. Windows packages are currently unsigned.

## Maintenance

The canonical writer is `goodtype-core` from a pinned Goodtype Git revision. Upgrade that pin
deliberately and retest notebook schema compatibility; do not copy or fork its persistence code.
Decoder and pypdf pins live in `requirements.txt`; the runtime URL/hash live in the build script.
Builds download these dependencies; conversion does not.

`cargo xtask verify` runs format, Clippy, Rust tests and synthetic Python/pipeline tests. The
Windows CI also builds a portable ZIP and tests it after extraction into another directory,
with development tools removed from PATH. This proves relocation on the runner, not validation
on every supported physical device. Private notebook samples are never CI fixtures or Git files.

`dev` and PR builds produce test artifacts. A successful push to `main` automatically publishes
the new `Cargo.toml` version after the Windows build and portable smoke check pass. Prerelease
version suffixes are respected. Existing tags/assets are never replaced. The release manifest
identifies the exact merged source commit. See [the release workflow](RELEASING.md).
