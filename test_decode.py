"""Redistributable synthetic checks; no personal notebook fixtures."""
import importlib.util
import io
import json
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile

TOOL = Path(__file__).resolve().parent

spec = importlib.util.spec_from_file_location("decode", Path(__file__).with_name("decode.py"))
decode = importlib.util.module_from_spec(spec)
spec.loader.exec_module(decode)


def zipped(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    stream.seek(0)
    return zipfile.ZipFile(stream)


class ArchiveTests(unittest.TestCase):
    def test_unsafe_paths_and_links(self):
        for name in ("../a", "/a", "C:/a", "a\\b", "a/./b", "a//b", "a:stream", "a\x00hidden"):
            with self.subTest(name=name), zipped([("safe", b"data")]) as archive:
                # ZipInfo normalizes these names when constructing a fixture on Windows.
                archive.infolist()[0].orig_filename = name
                with self.assertRaises(ValueError):
                    decode.checked_members(archive)
        link = zipfile.ZipInfo("link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipped([(link, b"elsewhere")]) as archive:
            with self.assertRaises(ValueError):
                decode.checked_members(archive)

    def test_duplicate_names_and_size_budgets(self):
        with zipped([("notes/a", b"a"), ("notes/A", b"b")]) as archive:
            with self.assertRaises(ValueError):
                decode.checked_members(archive)
        with zipped([("a", b"a")]) as archive:
            archive.infolist()[0].file_size = decode.MAX_MEMBER + 1
            with self.assertRaises(ValueError):
                decode.checked_members(archive)

    def test_backup_preserves_hierarchy_and_never_extracts_member_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backup.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("Physics/Lecture.goodnotes", b"notebook bytes")
                archive.writestr("Math/Exercises.goodnotes", b"other bytes")
            decode.inventory(source, root / "work")
            items = json.loads((root / "work/inventory.json").read_text())
            self.assertEqual(items[0]["folders"], ["Physics"])
            self.assertEqual(items[0]["name"], "Lecture")
            self.assertEqual(Path(items[0]["source"]).read_bytes(), b"notebook bytes")
            self.assertFalse((root / "work/Physics").exists())

    def test_unknown_backup_content_is_not_silently_omitted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backup.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("nested.zip", b"opaque")
            with self.assertRaises(ValueError):
                decode.inventory(source, root / "work")


class CompressionTests(unittest.TestCase):
    def test_literals_overlapping_matches_and_stored_frame_history(self):
        compressed = bytes([0x14]) + b"a" + bytes([1, 0])
        frame = b"bv41" + struct.pack("<II", 9, len(compressed)) + compressed + b"bv4$"
        self.assertEqual(decode.bounded_lz4(frame), (b"a"*9, len(frame)))
        stored = b"bv4-" + struct.pack("<II", 3, 3) + b"abc"
        second = b"\x02\x03\x00"
        frame = stored + b"bv41" + struct.pack("<II", 6, len(second)) + second + b"bv4$"
        self.assertEqual(decode.bounded_lz4(frame)[0], b"abcabcabc")

    def test_bounds_before_expansion(self):
        for data in (b"bv41" + struct.pack("<II", decode.MAX_FRAME+1, 0),
                     b"bv41" + struct.pack("<II", 1, 4) + b"\x1fA\x01\x00" + b"bv4$",
                     b"bv41" + struct.pack("<II", 1, 2) + b"\x10A",
                     b"bv41" + struct.pack("<II", 1, 4) + b"\x00\x00\x00\x00"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                decode.bounded_lz4(data)


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    value = value.encode() if isinstance(value, str) else value
    return varint(number*8+2) + varint(len(value)) + value


def synthetic_notebook():
    from pypdf import PdfWriter
    pdf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    writer.write(pdf)
    page = "11111111-1111-4111-8111-111111111111"
    template = "22222222-2222-4222-8222-222222222222"
    attachment = "33333333-3333-4333-8333-333333333333"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("schema.pb", bytes.fromhex("0818"))
        archive.writestr("index.notes.pb", field(1, page) + field(2, "notes/"+page))
        archive.writestr("index.attachments.pb", field(1, attachment) + field(2, "attachments/"+attachment))
        archive.writestr("index.events.pb", field(2, field(2, template)+field(4, attachment))
                         + field(54, field(2, page)+field(3, field(1, template))))
        archive.writestr("notes/"+page, b"")
        archive.writestr("attachments/"+attachment, pdf.getvalue())
    return stream.getvalue()


class PipelineTests(unittest.TestCase):
    def command(self, source, destination):
        binary = TOOL / "target/debug" / ("goodtype-goodnotes-import.exe" if sys.platform == "win32" else "goodtype-goodnotes-import")
        self.assertTrue(binary.is_file(), "run cargo test for the converter first")
        return subprocess.run([str(binary), str(source), str(destination)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

    def test_zip_to_normal_notebook_and_retry_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backup.zip"
            original = synthetic_notebook()
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("Engineering/Lecture.goodnotes", original)
            destination = root / "Imported"
            result = self.command(source, destination)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            notebook = destination / "Engineering/Lecture"
            manifest = json.loads((notebook/"goodtype.json").read_text())
            self.assertEqual(manifest["title"], "Lecture")
            self.assertEqual(manifest["pages"][0]["geometry"], {"widthPt":100.,"heightPt":200.})
            self.assertEqual((notebook/"originals/source.goodnotes").read_bytes(), original)
            self.assertNotEqual(self.command(source, destination).returncode, 0)
            self.assertEqual(json.loads((destination/"import-report.json").read_text())[0]["status"], "completed")

    def test_failed_notebook_does_not_damage_completed_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backup.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("Lecture.goodnotes", synthetic_notebook())
                archive.writestr("Corrupt.goodnotes", b"not a ZIP")
            destination = root / "Imported"
            self.assertNotEqual(self.command(source, destination).returncode, 0)
            self.assertTrue((destination/"Lecture/goodtype.json").is_file())
            self.assertFalse((destination/"Corrupt/goodtype.json").exists())
            report = json.loads((destination/"import-report.json").read_text())
            self.assertEqual([r["status"] for r in report], ["completed", "failed"])


if __name__ == "__main__":
    unittest.main()
