"""Build-boundary regression checks; no network or private notebook data."""
import importlib.util
import os
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location("builder", Path(__file__).with_name("build_windows.py"))
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
sign_spec = importlib.util.spec_from_file_location("signer", Path(__file__).with_name("sign_release.py"))
signer = importlib.util.module_from_spec(sign_spec)
sign_spec.loader.exec_module(signer)


class BuildTests(unittest.TestCase):
    def test_signing_rejects_dirty_or_unrelated_publishing_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            descriptor = root/"goodtype-goodnotes-importer-0.1.1-windows-x64.json"
            for dirty, commit in [(True, "expected"), (False, "other")]:
                descriptor.write_text(json.dumps({"sourceDirty":dirty,"sourceCommit":commit}))
                with patch.dict(os.environ, {"GITHUB_SHA":"expected"}), self.assertRaises(ValueError):
                    signer.sign(root, root/"public.pem", "private test placeholder")
                self.assertFalse((root/"importer-update.json").exists())

    def test_failed_signing_leaves_no_publishable_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"goodtype-goodnotes-importer-0.1.1-windows-x64.json").write_text(json.dumps({"sourceDirty":False,"sourceCommit":"expected"}))
            with patch.dict(os.environ, {"GITHUB_SHA":"expected"}), patch.object(signer.subprocess, "run", side_effect=RuntimeError("signer unavailable")):
                with self.assertRaises(RuntimeError):
                    signer.sign(root, root/"public.pem", "private test placeholder")
            self.assertFalse((root/"importer-update.json").exists())
            self.assertFalse((root/"importer-update.json.sig").exists())

    def test_publication_waits_out_a_brief_windows_file_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"source"
            source.mkdir()
            target = Path(directory)/"target"
            original = Path.rename
            attempts = []
            def rename(path, destination):
                attempts.append(path)
                if len(attempts) == 1:
                    raise PermissionError("transient scanner handle")
                return original(path,destination)
            with patch.object(Path,"rename",rename), patch.object(builder.time,"sleep"):
                builder.publish_folder(source,target)
            self.assertTrue(target.is_dir())
            self.assertEqual(len(attempts),2)

    def test_release_zip_accepts_epoch_dated_upstream_licenses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root/"portable"
            staging.mkdir()
            license = staging/"LICENSE"
            license.write_text("upstream license fixture")
            os.utime(license, (0,0))
            archive_path = root/"portable.zip"
            builder.zip_bundle(staging, archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(archive.read("portable/LICENSE"), b"upstream license fixture")
                self.assertEqual(archive.getinfo("portable/LICENSE").date_time[0], 1980)
            with self.assertRaises(FileExistsError):
                builder.zip_bundle(staging, archive_path)

    def test_runtime_archive_rejects_paths_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root/"runtime.zip"
            with zipfile.ZipFile(archive_path,"w") as archive:
                archive.writestr("../escaped", "unsafe")
            with self.assertRaises(ValueError):
                builder.unpack_runtime(archive_path, root/"runtime")
            self.assertFalse((root/"escaped").exists())

    def test_release_core_revision_matches_the_canonical_writer_dependency(self):
        manifest = tomllib.loads((builder.ROOT/"Cargo.toml").read_text())
        self.assertEqual(manifest["dependencies"]["goodtype-core"]["rev"], builder.CORE_REVISION)


if __name__ == "__main__":
    unittest.main()
