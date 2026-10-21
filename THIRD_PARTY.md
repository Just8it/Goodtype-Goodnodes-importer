# Optional decoder dependencies

- [parser-for-goodnotes](https://github.com/Kaih1825/parser-for-goodnotes), commit
  `66028455fd7fc61b7931679915d811a25485e3ad`, MIT. Installed separately; no upstream source
  files are vendored. Its package includes its license. Uses archive/wire/TPL decoding and
  SVG generation; CairoSVG/NumPy/PyMuPDF are deliberately not installed or called.
- [pypdf](https://github.com/py-pdf/pypdf), 6.21.1, BSD-3-Clause. Reads selected PDF page
  boxes/rotation. Installed separately with its license; no cryptography extra.
- Rust dependencies are isolated in this directory's Cargo manifest/lockfile: existing
  `goodtype-core` (pinned Goodtype Git revision) supplies canonical storage; serde/serde_json decode bounded interchange;
  tempfile stages work; sha2 fingerprints originals; uuid supplies new notebook identities;
  time records UTC creation times; roxmltree validates derived SVG with a restrictive allowlist;
  base64 reads embedded PNG/JPEG payloads; image decodes them with allocation/pixel bounds
  rather than accepting malformed compressed data from header checks alone; windows-sys applies the decoder process's memory
  ceiling and lifetime through a Windows Job Object. No desktop dependency or capability is added.

`decode.py`'s bounded LZ4 reader implements the documented LZ4 token/framing format independently;
all allocations are checked before expansion. Python never writes canonical notebook content.

Portable Windows packages additionally contain CPython 3.14.8 from python.org, with its original
license and bundled component notices. Its archive SHA-256 is pinned in `scripts/build_windows.py`.
Python decoder/pypdf distribution metadata and license files remain alongside the installed
packages. `licenses/rust/` contains the redistributed Rust dependencies' license/notice files;
`licenses/rust-dependencies.json` records their versions and SPDX license expressions.
