"""Build a portable Windows x64 release. Downloads happen only on the build machine."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.14.8"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
# Published by python.org on the 3.14.8 release page.
PYTHON_SHA256 = "a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310"
CORE_REVISION = "a670ba21375065da1e0a0826b28760c2d19623fc"
DECODER_REVISION = "66028455fd7fc61b7931679915d811a25485e3ad"
REPOSITORY = "https://github.com/Just8it/Goodtype-Goodnodes-importer"


def run(*args):
    subprocess.run(args, cwd=ROOT, check=True)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch_runtime(destination):
    with urllib.request.urlopen(PYTHON_URL, timeout=60) as response, destination.open("xb") as stream:
        total = 0
        while chunk := response.read(1024*1024):
            total += len(chunk)
            if total > 64*1024*1024:
                raise ValueError("Python runtime download exceeds 64 MiB")
            stream.write(chunk)
    if sha256(destination) != PYTHON_SHA256:
        raise ValueError(f"Python runtime checksum differs from the pinned python.org release: {sha256(destination)} ({destination.stat().st_size} bytes)")


def unpack_runtime(archive_path, destination):
    with zipfile.ZipFile(archive_path) as archive:
        if len(archive.infolist()) > 1000 or sum(i.file_size for i in archive.infolist()) > 256*1024*1024:
            raise ValueError("Python runtime archive exceeds limits")
        for info in archive.infolist():
            parts = info.filename.split("/")
            if any(p in ("", ".", "..") for p in parts) or any(c in info.filename for c in "\\:\x00"):
                raise ValueError("unsafe Python runtime path")
        archive.extractall(destination)


def collect_rust_licenses(destination):
    metadata = json.loads(subprocess.check_output(
        ["cargo", "metadata", "--locked", "--format-version", "1", "--filter-platform", "x86_64-pc-windows-msvc"], cwd=ROOT))
    catalog = []
    for package in metadata["packages"]:
        if package["name"] == "goodtype-goodnotes-import":
            continue
        source = Path(package["manifest_path"]).parent
        files = []
        for pattern in ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*"):
            files.extend(path for path in source.glob(pattern) if path.is_file())
        if not files and package["name"] == "goodtype-core":
            # Goodtype keeps its workspace license and notice above crates/goodtype-core.
            files = [source.parents[1]/"LICENSE", source.parents[1]/"NOTICE"]
        if package.get("license_file"):
            files.append(source/package["license_file"])
        if not files:
            raise ValueError(f"no distributable license found for {package['name']}")
        target = destination/"licenses/rust"/f"{package['name']}-{package['version']}"
        target.mkdir(parents=True)
        for path in sorted(set(files)):
            shutil.copy2(path, target/path.name)
        catalog.append({"name":package["name"], "version":package["version"],
                        "license":package.get("license"), "source":package.get("source")})
    (destination/"licenses/rust-dependencies.json").write_text(json.dumps(catalog, indent=2)+"\n", encoding="utf8")


def zip_bundle(staging, archive_path):
    # Upstream license files may use epoch timestamps, outside ZIP's 1980–2107 range.
    with zipfile.ZipFile(archive_path, "x", zipfile.ZIP_DEFLATED, strict_timestamps=False) as archive:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                archive.write(path, (Path(staging.name)/path.relative_to(staging)).as_posix())


def publish_folder(staging, destination):
    # Windows scanners can briefly retain a DLL handle after the runtime check exits.
    for attempt in range(20):
        try:
            staging.rename(destination)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.25)


def build(output):
    if sys.platform != "win32":
        raise ValueError("the portable release currently targets Windows x64")
    import platform
    if platform.machine().lower() not in ("amd64", "x86_64"):
        raise ValueError("build on a Windows x64 host")
    version = tomllib.loads((ROOT/"Cargo.toml").read_text(encoding="utf8"))["package"]["version"]
    name = f"goodtype-goodnotes-importer-{version}-windows-x64"
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    bundle = output/name
    archive_path = output/(name+".zip")
    descriptor_path = output/(name+".json")
    if any(p.exists() for p in (bundle, archive_path, descriptor_path)):
        raise ValueError("release output already exists; choose a fresh --output directory")
    run("cargo", "build", "--release", "--locked", "--bin", "goodtype-goodnotes-import")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    with tempfile.TemporaryDirectory(prefix=".package-", dir=output) as temporary:
        staging = Path(temporary)/name
        staging.mkdir()
        runtime = staging/"runtime"
        runtime_zip = Path(temporary)/"python.zip"
        cache = ROOT/"target/downloads"/f"python-{PYTHON_VERSION}-embed-amd64.zip"
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.exists():
            if sha256(cache) != PYTHON_SHA256:
                raise ValueError("cached Python runtime checksum differs; remove the damaged build cache")
            shutil.copy2(cache, runtime_zip)
        else:
            fetch_runtime(runtime_zip)
            shutil.copy2(runtime_zip, cache)
        unpack_runtime(runtime_zip, runtime)
        packages = runtime/"Lib/site-packages"
        run(sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "--no-compile",
            "--target", str(packages), "-r", str(ROOT/"requirements.txt"))
        # ._pth keeps the private runtime independent of system Python and user site packages.
        (runtime/"python314._pth").write_text("python314.zip\n.\nLib/site-packages\n", encoding="utf8")
        for source, target in [(ROOT/"target/release/goodtype-goodnotes-import.exe", "goodtype-goodnotes-import.exe"),
                               (ROOT/"decode.py", "decode.py"), (ROOT/"README.md", "README.md"),
                               (ROOT/"LICENSE", "LICENSE"), (ROOT/"THIRD_PARTY.md", "THIRD_PARTY.md")]:
            shutil.copy2(source, staging/target)
        collect_rust_licenses(staging)
        metadata = {"version":version, "protocolVersion":1, "notebookSchemaVersion":3,
                    "target":"x86_64-pc-windows-msvc", "sourceCommit":commit, "sourceDirty":dirty,
                    "goodtypeCoreRevision":CORE_REVISION, "decoderCommit":DECODER_REVISION,
                    "pythonVersion":PYTHON_VERSION, "pythonArchiveSha256":PYTHON_SHA256}
        metadata["files"] = [{"path":p.relative_to(staging).as_posix(), "sizeBytes":p.stat().st_size,
                              "sha256":sha256(p)} for p in sorted(staging.rglob("*")) if p.is_file()]
        (staging/"importer-manifest.json").write_text(json.dumps(metadata, indent=2)+"\n", encoding="utf8")
        run(str(staging/"goodtype-goodnotes-import.exe"), "--check")
        staged_archive = Path(temporary)/archive_path.name
        zip_bundle(staging, staged_archive)
        publish_folder(staging, bundle)
        staged_archive.rename(archive_path)
    descriptor = {k:v for k,v in metadata.items() if k != "files"}
    descriptor["archive"] = {"fileName":archive_path.name, "sizeBytes":archive_path.stat().st_size,
                             "sha256":sha256(archive_path),
                             "url":f"{REPOSITORY}/releases/download/v{version}/{archive_path.name}"}
    descriptor_path.write_text(json.dumps(descriptor, indent=2)+"\n", encoding="utf8")
    (output/"SHA256SUMS").write_text(f"{sha256(archive_path)}  {archive_path.name}\n{sha256(descriptor_path)}  {descriptor_path.name}\n", encoding="utf8")
    print(f"Portable release: {archive_path}")
    return archive_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT/"dist")
    arguments = parser.parse_args()
    build(arguments.output)
