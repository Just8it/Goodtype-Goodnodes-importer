[![status](https://img.shields.io/badge/status-alpha-red)](#alpha)
[![license](https://img.shields.io/badge/license-Apache%202.0-green)](./LICENSE)
[![platform](https://img.shields.io/badge/platform-Windows-blue)](#use)
[![built with](https://img.shields.io/badge/built%20with-Rust%20%2B%20Python-orange)](#build)

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/mark-dark.svg">
    <img src="brand/mark-light.svg" width="130" alt="Goodtype">
  </picture>
</p>

<h1 align="center">Goodtype Goodnotes Importer</h1>

<p align="center">Bring your Goodnotes notebooks into Goodtype. Keep your notes on your own disk.</p>

Convert a `.goodnotes` document or a backup ZIP into ordinary
[Goodtype](https://github.com/Just8it/Goodtype) notebooks. Folder structure, PDF backgrounds,
images and supported handwriting come with them. Open the result in Goodtype and organize it
with the existing Library.

Conversion works offline. No account, no upload, no telemetry. Goodtype works without this tool,
and imported notebooks stay usable if you remove it.

## Alpha

> [!WARNING]
> **The Goodnotes decoder is experimental. Keep your original exports and review the result.**
> Ordinary supported handwriting stays editable. Newer outline ink, dashed ink, shapes and
> cropped images use vector artwork with `--allow-derived`, which reduces editability.
> Text, sticky notes, audio, PDF stickers and partial erasure are not supported yet.

Each notebook keeps its source export in `originals/` and includes an import report with
page counts, artwork fallbacks and warnings. Existing destination folders are never replaced.

## Use

Download the **Windows x64 ZIP** from [Releases](https://github.com/Just8it/Goodtype-Goodnodes-importer/releases),
extract it and keep the whole folder together. It includes its own Python runtime.

```powershell
.\goodtype-goodnotes-import.exe 'C:\Notes\Backup.zip' 'C:\My Library\Imported Goodnotes' --allow-derived
```

The destination must be a new folder. Omit `--allow-derived` to reject notebooks that need
less-editable artwork. `--help` lists the options; `--check` verifies the decoder installation.
The first release is a standalone command-line tool; the in-app download/import menu comes next.

## Build

Requires Python 3.12+, the pinned Rust toolchain, and Visual Studio C++ Build Tools on Windows.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --no-deps -r requirements.txt
cargo xtask verify
.venv\Scripts\python.exe scripts/build_windows.py
```

The portable folder, ZIP and checksums appear in `dist/`. Dependencies are downloaded during
setup/build only. [Release integration](./INTEGRATION.md) describes the version contract and
future app installation boundary. [Development and releases](./RELEASING.md) explains how
`dev` is promoted to `main`, where GitHub builds and publishes new versions automatically.

## License

Apache License 2.0 — see [LICENSE](./LICENSE). The decoder is MIT-licensed; pypdf is BSD-licensed.
[THIRD_PARTY.md](./THIRD_PARTY.md) records the pinned components. Release bundles include their
licenses and the Python runtime license. This project is not affiliated with Goodnotes.
