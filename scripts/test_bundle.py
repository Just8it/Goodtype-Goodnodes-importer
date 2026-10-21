"""Smoke-test an extracted release in a different directory with no development tools on PATH."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def test_bundle(archive_path):
    spec = importlib.util.spec_from_file_location("synthetic", ROOT/"test_decode.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    with tempfile.TemporaryDirectory(prefix="goodtype portable test ") as directory:
        root = Path(directory)
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(root)
        bundles = list(root.glob("goodtype-goodnotes-importer-*"))
        assert len(bundles) == 1
        bundle = bundles[0]
        manifest = json.loads((bundle/"importer-manifest.json").read_text())
        for entry in manifest["files"]:
            path = bundle/entry["path"]
            assert path.stat().st_size == entry["sizeBytes"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        environment = os.environ.copy()
        environment["PATH"] = str(Path(os.environ["SystemRoot"])/"System32")
        environment["PYTHONHOME"] = str(root/"missing-system-python")
        environment["PYTHONPATH"] = str(root/"missing-system-packages")
        binary = bundle/"goodtype-goodnotes-import.exe"

        def command(*args, success=True):
            result = subprocess.run([str(binary), *map(str,args)], cwd=root, env=environment,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            assert (result.returncode == 0) == success, result.stderr.decode(errors="replace")
            return result.stdout.decode()

        info = json.loads(command("--version", "--json"))
        assert info["protocolVersion"] == 1 and info["notebookSchemaVersion"] == 3
        assert info["version"] == manifest["version"]
        assert json.loads(command("--check"))["pythonVersion"] == manifest["pythonVersion"]
        source = root/"Lecture.goodnotes"
        original = fixture.synthetic_notebook()
        source.write_bytes(original)
        destination = root/"Imported notebooks"
        command(source, destination)
        notebook = destination/"Lecture"
        data = json.loads((notebook/"goodtype.json").read_text())
        assert data["title"] == "Lecture" and len(data["pages"]) == 1
        assert (notebook/"originals/source.goodnotes").read_bytes() == original
        command(source, destination, success=False)
        moved = root/"Sorted lecture"
        notebook.rename(moved)
        assert (moved/"references").is_dir()
        assert json.loads((moved/"pages/page-001.json").read_text())["geometry"] == {"widthPt":100.,"heightPt":200.}
        print("Portable smoke passed: relocated bundle, isolated PATH, runtime check, import, source retention, retry refusal and move.")


if __name__ == "__main__":
    test_bundle(Path(sys.argv[1]).resolve())
