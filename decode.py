"""Optional decoder. Writes scratch interchange files, never Goodtype notebooks."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
import re
from pathlib import Path
import stat
import struct
import sys
import zipfile

PIN = "66028455fd7fc61b7931679915d811a25485e3ad"
MIB = 1024 * 1024
MAX_MEMBER = 128 * MIB
MAX_ARCHIVE = 512 * MIB
MAX_FRAME = 16 * MIB
SCALE = 72 / 132


def checked_members(archive):
    infos = archive.infolist()
    if len(infos) > 10_000 or sum(i.file_size for i in infos) > MAX_ARCHIVE:
        raise ValueError("archive exceeds member/expanded-byte limit")
    seen = set()
    for info in infos:
        name = info.orig_filename.rstrip("/")
        parts = name.split("/")
        if (not name or any(p in ("", ".", "..") for p in parts)
                or any(c in name for c in "\\:\x00") or name.startswith("/")
                or info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16)
                or info.file_size > MAX_MEMBER or name.casefold() in seen):
            raise ValueError(f"unsafe, duplicate, encrypted or oversized ZIP member: {name!r}")
        seen.add(name.casefold())
    return infos


def read_member(archive, name):
    with archive.open(name) as stream:
        data = stream.read(MAX_MEMBER + 1)
    if len(data) > MAX_MEMBER:
        raise ValueError("actual ZIP member exceeds byte limit")
    return data


def bounded_lz4(data):
    """Apple framing with bounds checked before every LZ4 output expansion."""
    out = bytearray()
    pos = 0
    while pos + 4 <= len(data):
        magic = data[pos:pos + 4]
        pos += 4
        if magic == b"bv4$":
            return bytes(out), pos
        if magic not in (b"bv41", b"bv4-") or pos + 8 > len(data):
            raise ValueError("invalid LZ4 frame")
        expected, stored = struct.unpack_from("<II", data, pos)
        pos += 8
        if expected > MAX_FRAME - len(out) or pos + stored > len(data):
            raise ValueError("oversized or truncated LZ4 frame")
        source = data[pos:pos + stored]
        pos += stored
        if magic == b"bv4-":
            if len(source) != expected:
                raise ValueError("stored LZ4 size mismatch")
            out.extend(source)
            continue
        start = len(out)
        cursor = 0

        def length(value):
            nonlocal cursor
            if value == 15:
                while True:
                    if cursor >= len(source):
                        raise ValueError("truncated LZ4 length")
                    extension = source[cursor]
                    cursor += 1
                    value += extension
                    if extension != 255:
                        break
            return value

        while cursor < len(source):
            token = source[cursor]
            cursor += 1
            count = length(token >> 4)
            if cursor + count > len(source) or len(out) - start + count > expected:
                raise ValueError("invalid LZ4 literals")
            out.extend(source[cursor:cursor + count])
            cursor += count
            if cursor == len(source):
                break
            if cursor + 2 > len(source):
                raise ValueError("truncated LZ4 offset")
            offset = int.from_bytes(source[cursor:cursor + 2], "little")
            cursor += 2
            count = length(token & 15) + 4
            if not 0 < offset <= min(65536, len(out)) or len(out) - start + count > expected:
                raise ValueError("invalid LZ4 match")
            for _ in range(count):
                out.append(out[-offset])
        if len(out) - start != expected:
            raise ValueError("LZ4 size mismatch")
    raise ValueError("missing LZ4 end marker")


def inventory(source, output):
    """Expand only notebook entries of a backup; never arbitrary nested archives."""
    if source.stat().st_size > MAX_ARCHIVE:
        raise ValueError("input exceeds 512 MiB")
    output.mkdir()
    items = []
    with zipfile.ZipFile(source) as archive:
        infos = checked_members(archive)
        if source.suffix.lower() == ".goodnotes":
            items.append({"name": source.stem, "folders": [], "source": str(source)})
        else:
            unknown = [i.filename for i in infos if not i.is_dir()
                       and not i.filename.lower().endswith(".goodnotes")
                       and not i.filename.startswith("__MACOSX/")]
            if unknown:
                raise ValueError(f"backup contains unsupported files: {unknown[:5]}")
            for info in infos:
                if info.is_dir() or not info.filename.lower().endswith(".goodnotes"):
                    continue
                data = read_member(archive, info.filename)
                path = output / f"source-{len(items):04}.goodnotes"
                path.write_bytes(data)
                parts = info.filename.split("/")
                items.append({"name": Path(parts[-1]).stem, "folders": parts[:-1],
                              "source": str(path), "original_path": info.filename})
    if not items or len(items) > 500:
        raise ValueError("expected 1–500 Goodnotes notebooks")
    (output / "inventory.json").write_text(json.dumps(items), encoding="utf8")


def check_installation():
    from importlib.metadata import distribution, version
    installed = json.loads(distribution("goodnotes-document-parser").read_text("direct_url.json") or "{}")
    if installed.get("vcs_info", {}).get("commit_id") != PIN or version("pypdf") != "6.21.1":
        raise ValueError("decoder installation differs from requirements.txt; recreate its environment")
    return {"decoderCommit": PIN, "pypdfVersion": version("pypdf"), "pythonVersion": sys.version.split()[0]}


def decode(source, output):
    check_installation()
    from goodnotes_re import GoodNotesDocument, page_to_svg
    import goodnotes_re.archive as archive_module
    import goodnotes_re.page as page_module
    import goodnotes_re.stroke as stroke_module
    from pypdf import PdfReader

    output.mkdir()
    output_bytes = 0

    def emit(filename, data, maximum):
        nonlocal output_bytes
        if len(data) > maximum or output_bytes + len(data) > MAX_ARCHIVE:
            raise ValueError("decoded output exceeds file/job byte limit")
        (output / filename).write_bytes(data)
        output_bytes += len(data)

    failures = []
    cache = {}
    current = {}
    original_records = GoodNotesDocument.decode_records
    original_stroke = page_module.parse_stroke_field
    original_page = archive_module.parse_page_from_records
    stroke_module.decode_apple_lz4 = bounded_lz4

    def read(doc, member):
        if member not in cache:
            data = read_member(doc._archive, member)
            if sum(map(len, cache.values())) + len(data) > MAX_ARCHIVE:
                raise ValueError("actual expanded archive exceeds byte limit")
            cache[member] = data
        return cache[member]

    def records(doc, member):
        try:
            return original_records(doc, member)
        except Exception as error:
            failures.append(f"{member}: {type(error).__name__}: {error}")
            raise

    def stroke(*args, **kwargs):
        try:
            result = original_stroke(*args, **kwargs)
            if not result:
                current.setdefault("empty", []).append(args[0])
            return result
        except Exception as error:
            failures.append(f"stroke: {type(error).__name__}: {error}")
            raise

    assets = {}
    pdfs = {}

    def asset(doc, member):
        if member in assets:
            return assets[member]
        data = doc.read(member)
        ext = ("pdf" if data.startswith(b"%PDF") else "png" if data.startswith(b"\x89PNG\r\n\x1a\n")
               else "jpg" if data.startswith(b"\xff\xd8\xff") else None)
        if ext is None:
            raise ValueError(f"unsupported attachment: {member}")
        filename = hashlib.sha256(data).hexdigest() + "." + ext
        emit(filename, data, MAX_MEMBER)
        assets[member] = filename
        return filename

    def page(*args, **kwargs):
        current.clear()
        p = original_page(*args, **kwargs)
        if len(p.strokes) > 20_000 or sum(len(s.points) for s in p.strokes) > 750_000:
            raise ValueError("page exceeds Goodtype ink limits")
        shape_ids = {s.uuid for s in p.shapes}
        if any(u not in shape_ids for u in current.get("empty", [])):
            raise ValueError(f"page {p.index+1}: undecoded compressed ink")
        meaningful_fragments = [f for f in p.text_fragments
                                if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", f.text)]
        if p.text_elements or meaningful_fragments or p.sticky_notes:
            raise ValueError(f"page {p.index+1}: text/sticky notes are not supported yet")
        warnings = []
        if any(s.parent_uuid for s in p.strokes) or any(s.parent_uuid for s in p.shapes):
            warnings.append("Source grouping flattened; decoder's final object positions retained")
        background = None
        width, height = p.dimensions.width, p.dimensions.height
        if p.background_attachment_path:
            background = asset(doc, p.background_attachment_path)
            if not background.endswith(".pdf"):
                raise ValueError("non-PDF page backgrounds are not supported yet")
            if background not in pdfs:
                reader = PdfReader(io.BytesIO(doc.read(p.background_attachment_path)), strict=True)
                if reader.is_encrypted or len(reader.pages) > 2000:
                    raise ValueError("encrypted/oversized PDF background")
                pdfs[background] = reader
            reader = pdfs[background]
            if not 1 <= p.pdf_page_index <= len(reader.pages):
                raise ValueError("background PDF page index is out of range")
            pdf_page = reader.pages[p.pdf_page_index - 1]
            box = pdf_page.cropbox
            width, height = float(box.width), float(box.height)
            unit = float(pdf_page.get("/UserUnit", 1))
            if unit != 1 or float(box.left) != 0 or float(box.bottom) != 0:
                raise ValueError("PDF background with shifted origin/UserUnit needs geometry validation")
            if pdf_page.rotation % 180:
                width, height = height, width
            if max(abs(width-p.dimensions.width), abs(height-p.dimensions.height)) > .1:
                warnings.append("PDF page geometry corrected using the selected page box")
        else:
            raise ValueError("page has no verified background/geometry; refusing guessed Letter paper")
        p = replace(p, dimensions=replace(p.dimensions, width=width, height=height),
                    background_attachment_path=None)
        ink = []
        images = []
        derived = []

        def artwork(strokes=(), shapes=(), image_elements=(), reason=""):
            view = replace(p, strokes=list(strokes), shapes=list(shapes), image_elements=tuple(image_elements),
                           elements=(), sticky_notes=(), text_fragments=[], text_elements=())
            svg = page_to_svg(view, doc)
            filename = f"page-{p.index+1:04}-derived-{len(derived):04}.svg"
            emit(filename, svg.encode("utf8"), 20*MIB)
            derived.append({"file": filename, "z": len(ink)+len(images)+len(derived),
                            "reason": reason, "source_count": len(strokes)+len(shapes)+len(image_elements)})

        for image in p.image_elements:
            member = "attachments/" + image.attachment_uuid
            filename = asset(doc, member)
            if filename.endswith(".pdf"):
                raise ValueError("PDF stickers are not supported yet")
            cropped = image.orig_width > 0 and image.orig_height > 0 and (
                abs(image.width-image.orig_width) > .1 or abs(image.height-image.orig_height) > .1)
            if cropped or abs(image.rotation_rad) > .00001:
                artwork(image_elements=[image], reason="cropped/rotated image")
            else:
                images.append({"file": filename, "x": image.x*SCALE, "y": image.y*SCALE,
                               "width": image.width*SCALE, "height": image.height*SCALE,
                               "z": len(ink)+len(images)+len(derived)})
        native_run = []

        def flush_native():
            if native_run:
                artwork(strokes=native_run, reason="native outline ink")
                native_run.clear()

        for s in p.strokes:
            if s.eraser_cuts or s.is_cut_start or s.is_cut_end:
                raise ValueError(f"page {p.index+1}: partially erased ink needs fidelity validation")
            if s.dash_pattern:
                flush_native()
                artwork(strokes=[s], reason="dashed ink")
                continue
            if s.native_cgpaths or s.outline_polygons or s.ribbon_path:
                native_run.append(s)
                continue
            flush_native()
            radii = [pt.pressure*SCALE for pt in s.points]
            widths = [2*r if r > 0 else s.width*SCALE for r in radii]
            if not widths or min(widths) <= 0 or max(widths) > 4*min(widths):
                artwork(strokes=[s], reason="ink width outside native pressure range")
                continue
            nominal = max(widths)
            pressure = max(widths)-min(widths) > .000001
            ink.append({"color": s.color_hex, "opacity": s.alpha, "width": nominal,
                        "highlighter": s.is_highlighter, "pressure": pressure,
                        "z": len(ink)+len(images)+len(derived),
                        "points": [[pt.x*SCALE, pt.y*SCALE, (w/nominal-.25)/.75 if pressure else 1]
                                   for pt, w in zip(s.points, widths)]})
        flush_native()
        if p.shapes:
            artwork(shapes=p.shapes, reason="recognized vector shapes")
        data = {"width": width, "height": height, "background": background,
                "pdf_page": p.pdf_page_index, "ink": ink, "images": images,
                "derived": derived, "warnings": warnings, "source_uuid": p.uuid,
                "source_strokes": len(p.strokes), "source_shapes": len(p.shapes),
                "source_images": len(p.image_elements)}
        filename = f"page-{p.index+1:04}.json"
        emit(filename, json.dumps(data, allow_nan=False, separators=(",", ":")).encode("utf8"), 64*MIB)
        pages.append(filename)
        print(f"Decoded page {p.index+1}: {len(ink)} editable strokes, {len(derived)} vector groups", flush=True)
        # The upstream assembler retains Page objects; release the large geometry now.
        return replace(p, strokes=[], shapes=[], image_elements=(), elements=(), text_fragments=[])

    GoodNotesDocument.read = read
    GoodNotesDocument.decode_records = records
    page_module.parse_stroke_field = stroke
    archive_module.parse_page_from_records = page
    pages = []
    with GoodNotesDocument.open(source) as doc:
        checked_members(doc._archive)
        schema = doc.read("schema.pb").hex()
        if schema not in ("0818", "081f"):
            raise ValueError(f"untested Goodnotes schema: {schema}")
        for required in ("index.notes.pb", "index.events.pb", "index.attachments.pb"):
            if required not in doc.member_names():
                raise ValueError(f"missing source index: {required}")
            doc.decode_records(required)
        doc.pages()
        if failures or not pages or len(pages) > 2000:
            raise ValueError(f"incomplete decode: {failures[:5]}")
        # Audio and opaque attachments remain in the original, but cannot silently disappear.
        for member in doc.member_names():
            if member.startswith("attachments/"):
                data = doc.read(member)
                if not data.startswith((b"%PDF", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
                    raise ValueError(f"unsupported attachment (possibly audio): {member}")
    result = {"version": 1, "decoder": PIN, "schema": schema, "pages": pages,
              "warnings": ["Experimental source decoder; mixed-object stacking and source fidelity require visual review."]}
    emit("decoded.json", json.dumps(result).encode("utf8"), MIB)


if __name__ == "__main__":
    sys.setrecursionlimit(256)
    try:
        if sys.platform != "win32":
            import resource
            limit = 1536*MIB
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        if sys.argv[1:] == ["check"]:
            print(json.dumps(check_installation()))
            sys.exit(0)
        mode, source, output = sys.argv[1:]
        if mode == "inventory":
            inventory(Path(source), Path(output))
        elif mode == "decode":
            decode(Path(source), Path(output))
        else:
            raise ValueError("unknown decoder mode")
    except Exception as error:
        print(f"Goodnotes decoder: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
