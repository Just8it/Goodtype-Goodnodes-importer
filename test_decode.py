"""Redistributable synthetic checks; no personal notebook fixtures."""
import importlib.util
import io
import json
import math
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

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


class GeometryTests(unittest.TestCase):
    def rendered(self, **kwargs):
        from goodnotes_re import page_to_svg
        from goodnotes_re.page import Page, PageDimensions
        from goodnotes_re.shape import ShapePath
        shape = ShapePath(record_index=0, uuid="synthetic", points=((10., 20.), (110., 20.)),
                          stroke_width=2., field_numbers=(1,), is_filled=False, **kwargs)
        page = Page(index=0, uuid="page", member_path="notes/page", dimensions=PageDimensions(600, 800), shapes=[shape])
        return shape, page_to_svg(page, None)

    def test_native_line_has_local_geometry_without_smoothing(self):
        shape, svg = self.rendered()
        result = decode.native_shape(shape, svg)
        self.assertEqual(result["geometry"]["kind"], "line")
        self.assertAlmostEqual(result["x"], round(10*decode.SCALE, 2))
        self.assertEqual(result["geometry"]["start"], {"x": 0., "y": 0.})
        self.assertAlmostEqual(result["geometry"]["end"]["x"], round(110*decode.SCALE, 2)-round(10*decode.SCALE, 2))

    def test_curves_keep_exact_cubic_and_quadratic_controls(self):
        result = decode.path_geometry("M 10 20 C 12 25 25 28 30 30 Q 40 50 60 60")
        self.assertEqual(result["kind"], "spline")
        nodes = result["nodes"]
        self.assertEqual(nodes[0]["handleOut"], {"x": 2., "y": 5.})
        self.assertEqual(nodes[1]["handleIn"], {"x": -5., "y": -2.})
        self.assertAlmostEqual(nodes[1]["handleOut"]["x"], 20/3)
        self.assertAlmostEqual(nodes[2]["handleIn"]["y"], -20/3)
        self.assertIsNone(decode.path_geometry("M 0 0 L 1 1 M 2 2 L 3 3"))
        self.assertIsNone(decode.path_geometry("M 0 0 L 0 0"))
        self.assertIsNone(decode.path_geometry("M 0 0 A 1 1 0 0 0 2 2"))
        with self.assertRaises(ValueError):
            decode.path_geometry("M 0 0 L 1e999 2")

    def test_closed_and_rounded_paths_keep_the_closing_handle(self):
        result = decode.path_geometry("M 0 0 L 20 0 L 20 20 Q 10 30 0 0 Z")
        self.assertEqual(len(result["nodes"]), 3)
        self.assertTrue(result["closed"])
        self.assertAlmostEqual(result["nodes"][0]["handleIn"]["x"], 20/3)

    def test_rotated_ellipse_retains_centre_and_uses_degrees(self):
        shape, svg = self.rendered(shape_type="ellipse", cx=100., cy=200., rx=40., ry=20., rotation=math.pi/2)
        result = decode.native_shape(shape, svg)
        self.assertEqual(result["rotation"], 90.)
        g = result["geometry"]
        self.assertAlmostEqual(result["x"]-g["heightPt"]/2, round(100*decode.SCALE, 2))
        self.assertAlmostEqual(result["y"]+g["widthPt"]/2, round(200*decode.SCALE, 2))

    def test_unrepresentable_style_stays_vector_artwork(self):
        for kwargs in ({"start_arrow": 1}, {"dash_pattern": (4., 5.)}):
            shape, svg = self.rendered(**kwargs)
            self.assertIsNone(decode.native_shape(shape, svg))
        shape, svg = self.rendered()
        self.assertIsNone(decode.native_shape(shape, svg.replace('fill="none"', 'fill="#000000" fill-opacity="0.3"')))

    def test_vector_bounds_include_controls_stroke_and_image_crop(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="800"><path d="M 10 20 C -5 0 40 80 50 20" stroke="#000000" stroke-width="4"/></svg>'
        box = decode.svg_bounds(svg)
        self.assertLess(box[0], -5)
        self.assertGreater(box[3], 80)
        cropped = '<svg xmlns="http://www.w3.org/2000/svg"><g transform="rotate(90 20 30)"><svg x="10" y="10" width="20" height="40" overflow="hidden"><image x="-100" y="-100" width="1000" height="1000"/></svg></g></svg>'
        box = decode.svg_bounds(cropped)
        self.assertEqual(box, (-1., 19., 41., 41.))
        data, placement = decode.crop_artwork([(svg, decode.svg_bounds(svg))], 600, 800)
        root = ET.fromstring(data)
        self.assertEqual(float(root.get("width")), placement[2])
        self.assertLess(placement[2], 100)
        self.assertLess(placement[3], 100)
        self.assertEqual(len(root), 1)

    def test_clustering_joins_neighbours_and_limits_chains(self):
        group = [("a", (10, 10, 20, 20))]
        self.assertTrue(decode.nearby_artwork(group, (24, 15, 30, 25)))
        self.assertFalse(decode.nearby_artwork(group, (200, 400, 220, 450)))
        group.append(("b", (15, 170, 25, 185)))
        self.assertFalse(decode.nearby_artwork(group, (15, 188, 25, 200)))

    def test_arc_bounds_allow_svg_radius_expansion(self):
        box = decode.svg_bounds('<svg><path d="M 0 0 A 1 1 0 0 1 100 0"/></svg>')
        self.assertLessEqual(box[1], -50)
        self.assertGreaterEqual(box[3], 50)
        with self.assertRaises(ValueError):
            decode.svg_bounds('<svg><path d="M 0 0 A 2 1 0 0 1 100 0"/></svg>')

    def test_clustering_merges_spatial_neighbours_without_reordering_overlaps(self):
        fragments = [("left", (0, 0, 10, 10)), ("right", (200, 0, 210, 10)),
                     ("left-again", (12, 0, 20, 10))]
        groups = decode.cluster_artwork(fragments)
        self.assertEqual([[name for name, _ in group] for group in groups], [["left", "left-again"], ["right"]])
        # The last mark is closer to the first but crosses the long intervening mark.
        fragments = [("first", (0, 0, 10, 10)), ("middle", (10, 0, 200, 10)),
                     ("last", (9, 0, 12, 10))]
        groups = decode.cluster_artwork(fragments)
        self.assertEqual([[name for name, _ in group] for group in groups], [["first"], ["middle"], ["last"]])


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
