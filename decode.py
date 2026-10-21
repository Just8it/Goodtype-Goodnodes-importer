"""Optional decoder. Writes scratch interchange files, never Goodtype notebooks."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
import math
import re
from pathlib import Path
import stat
import struct
import sys
import zipfile
import xml.etree.ElementTree as ET

PIN = "66028455fd7fc61b7931679915d811a25485e3ad"
MIB = 1024 * 1024
MAX_MEMBER = 128 * MIB
MAX_ARCHIVE = 512 * MIB
MAX_FRAME = 16 * MIB
SCALE = 72 / 132
SVG_NS = "http://www.w3.org/2000/svg"
NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
ET.register_namespace("", SVG_NS)


def path_commands(data):
    """Only the absolute commands emitted by the pinned renderer, with bounded numbers."""
    tokens = re.findall(r"[MLCQAZ]|" + NUMBER, data)
    if re.sub(r"[MLCQAZ]|" + NUMBER + r"|[\s,]", "", data):
        raise ValueError("unsupported generated SVG path")
    counts = {"M": 2, "L": 2, "C": 6, "Q": 4, "A": 7, "Z": 0}
    commands = []
    i = 0
    while i < len(tokens):
        op = tokens[i]
        count = counts.get(op)
        if count is None or i + count >= len(tokens):
            raise ValueError("incomplete generated SVG path")
        args = tuple(float(v) for v in tokens[i+1:i+1+count])
        if any(not math.isfinite(v) or abs(v) > 10_000_000 for v in args):
            raise ValueError("generated SVG coordinate exceeds bounds")
        commands.append((op, args))
        i += count + 1
    return commands


def path_geometry(data):
    """Convert line/quadratic/cubic paths to existing knots; never fit or smooth them."""
    nodes = []
    closed = False
    for op, args in path_commands(data):
        if op == "M":
            if nodes:
                return None  # Multiple subpaths cannot share one canonical spline.
            nodes.append({"point": {"x": args[0], "y": args[1]}, "handleIn": None, "handleOut": None})
        elif op in ("L", "Q", "C") and nodes and not closed:
            end = {"x": args[-2], "y": args[-1]}
            incoming = None
            if op != "L":
                start = nodes[-1]["point"]
                if op == "Q":
                    c1 = [start[k] + 2/3*(args[n]-start[k]) for n, k in enumerate(("x", "y"))]
                    c2 = [end[k] + 2/3*(args[n]-end[k]) for n, k in enumerate(("x", "y"))]
                else:
                    c1, c2 = args[:2], args[2:4]
                nodes[-1]["handleOut"] = {k: c1[n]-start[k] for n, k in enumerate(("x", "y"))}
                incoming = {k: c2[n]-end[k] for n, k in enumerate(("x", "y"))}
            nodes.append({"point": end, "handleIn": incoming, "handleOut": None})
        elif op == "Z" and nodes:
            closed = True
        else:
            return None
    if closed and len(nodes) > 1 and nodes[0]["point"] == nodes[-1]["point"]:
        nodes[0]["handleIn"] = nodes.pop()["handleIn"]
    if not 2 + int(closed) <= len(nodes) <= 256:
        return None
    if len(nodes) == 2 and not closed and not any(n["handleIn"] or n["handleOut"] for n in nodes):
        if nodes[0]["point"] == nodes[1]["point"]:
            return None
        return {"kind": "line", "start": nodes[0]["point"], "end": nodes[1]["point"]}
    return {"kind": "spline", "nodes": nodes, "closed": closed}


def native_shape(shape, svg):
    """Only promote geometry whose appearance the canonical shape can represent."""
    if shape.start_arrow or shape.end_arrow or shape.dash_pattern:
        return None
    root = ET.fromstring(svg)
    elements = list(root)
    if len(elements) != 1:
        return None
    element = elements[0]
    tag = element.tag.rsplit("}", 1)[-1]
    x = y = rotation = 0.
    filled = element.get("fill", "none") != "none"
    opacity = float(element.get("stroke-opacity", "1"))
    if filled and (float(element.get("fill-opacity", "1")) != 1 or opacity != 1):
        return None  # Separate fill/outline alpha composites differently from object opacity.
    style = {"strokeColor": shape.color_hex, "strokeWidthPt": float(element.get("stroke-width", "1")),
             "fillColor": shape.color_hex if filled else None, "opacity": opacity}
    if tag == "ellipse":
        width, height = 2*float(element.get("rx")), 2*float(element.get("ry"))
        rotation = shape.rotation
        # Object rotation is about its local origin; retain the source ellipse centre.
        c, s = math.cos(rotation), math.sin(rotation)
        x = float(element.get("cx")) - c*width/2 + s*height/2
        y = float(element.get("cy")) - s*width/2 - c*height/2
        geometry = {"kind": "ellipse", "widthPt": width, "heightPt": height}
    elif tag == "rect":
        x, y = float(element.get("x", "0")), float(element.get("y", "0"))
        geometry = {"kind": "rectangle", "widthPt": float(element.get("width")),
                    "heightPt": float(element.get("height")), "cornerRadiusPt": float(element.get("rx", "0"))}
    elif tag == "line":
        geometry = {"kind": "line", "start": {"x": float(element.get("x1")), "y": float(element.get("y1"))},
                    "end": {"x": float(element.get("x2")), "y": float(element.get("y2"))}}
        if geometry["start"] == geometry["end"]:
            return None
    elif tag == "path":
        geometry = path_geometry(element.get("d", ""))
        if geometry is None:
            return None
    else:
        return None
    if geometry["kind"] in ("rectangle", "ellipse"):
        if not all(0 < geometry[k] <= 14_400 for k in ("widthPt", "heightPt")):
            return None
    else:
        points = ([geometry["start"], geometry["end"]] if geometry["kind"] == "line"
                  else [n["point"] for n in geometry["nodes"]])
        x, y = min(p["x"] for p in points), min(p["y"] for p in points)
        for point in points:
            point["x"] -= x
            point["y"] -= y
    return {"x": x, "y": y, "rotation": math.degrees(rotation), "geometry": geometry, "style": style}


def union_bounds(boxes):
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def multiply(a, b):
    return (a[0]*b[0]+a[2]*b[1], a[1]*b[0]+a[3]*b[1],
            a[0]*b[2]+a[2]*b[3], a[1]*b[2]+a[3]*b[3],
            a[0]*b[4]+a[2]*b[5]+a[4], a[1]*b[4]+a[3]*b[5]+a[5])


def transform_matrix(value):
    result = (1., 0., 0., 1., 0., 0.)
    for match in re.finditer(r"(translate|rotate|scale|matrix)\s*\(([^)]*)\)", value):
        name, raw = match.groups()
        args = [float(v) for v in re.findall(NUMBER, raw)]
        if name == "translate" and len(args) in (1, 2):
            matrix = (1, 0, 0, 1, args[0], args[1] if len(args) == 2 else 0)
        elif name == "scale" and len(args) in (1, 2):
            matrix = (args[0], 0, 0, args[-1], 0, 0)
        elif name == "rotate" and len(args) in (1, 3):
            angle = math.radians(args[0])
            c, s = math.cos(angle), math.sin(angle)
            x, y = args[1:] if len(args) == 3 else (0, 0)
            matrix = (c, s, -s, c, x-c*x+s*y, y-s*x-c*y)
        elif name == "matrix" and len(args) == 6:
            matrix = tuple(args)
        else:
            raise ValueError("unsupported generated SVG transform")
        result = multiply(result, matrix)
    if re.sub(r"(translate|rotate|scale|matrix)\s*\([^)]*\)|[\s,]", "", value):
        raise ValueError("unsupported generated SVG transform")
    return result


def svg_bounds(svg):
    """Conservative vector bounds, including curve control hulls, strokes and cropped images."""
    def visit(node, parent, inherited_width=0):
        tag = node.tag.rsplit("}", 1)[-1]
        matrix = multiply(parent, transform_matrix(node.get("transform", "")))
        width = float(node.get("stroke-width", inherited_width))
        if tag == "g":
            return union_bounds(visit(child, matrix, width) for child in node)
        if tag == "defs":
            return None
        n = lambda key, default=0: float(node.get(key, default))
        points = []
        if tag in ("rect", "image", "svg"):
            x, y, w, h = n("x"), n("y"), n("width"), n("height")
            points = [(x, y), (x+w, y), (x+w, y+h), (x, y+h)]
        elif tag in ("ellipse", "circle"):
            x, y = n("cx"), n("cy")
            rx, ry = (n("r"), n("r")) if tag == "circle" else (n("rx"), n("ry"))
            points = [(x-rx, y-ry), (x+rx, y-ry), (x+rx, y+ry), (x-rx, y+ry)]
        elif tag == "line":
            points = [(n("x1"), n("y1")), (n("x2"), n("y2"))]
        elif tag in ("polygon", "polyline"):
            values = [float(v) for v in re.findall(NUMBER, node.get("points", ""))]
            points = list(zip(values[::2], values[1::2]))
        elif tag == "path":
            current = start = None
            for op, args in path_commands(node.get("d", "")):
                if op == "A":
                    rx, ry, angle, _, _, x, y = args
                    if current is None or abs(rx-ry) > .00001 or angle != 0:
                        raise ValueError("unverified generated elliptical arc")
                    # SVG expands a too-small circle to half its endpoint distance.
                    radius = 2*max(abs(rx), math.hypot(x-current[0], y-current[1])/2)
                    points.extend([(x-radius, y-radius), (x+radius, y+radius)])
                    current = (x, y)
                else:
                    points.extend(zip(args[::2], args[1::2]))
                    if args:
                        current = args[-2:]
                    if op == "M":
                        start = current
                    elif op == "Z":
                        current = start
        else:
            raise ValueError(f"cannot bound generated SVG element: {tag}")
        if not points:
            return None
        a, b, c, d, e, f = matrix
        points = [(a*x+c*y+e, b*x+d*y+f) for x, y in points]
        # One extra point covers the renderer's two-decimal rounding and antialiasing edge.
        padding = 1 + width*math.hypot(a, b, c, d)/2
        if node.get("marker-start") or node.get("marker-end"):
            padding += 12*width
        box = (min(x for x, _ in points)-padding, min(y for _, y in points)-padding,
               max(x for x, _ in points)+padding, max(y for _, y in points)+padding)
        if any(not math.isfinite(v) or abs(v) > 10_000_000 for v in box):
            raise ValueError("generated SVG bounds exceed coordinate limits")
        return box

    root = ET.fromstring(svg)
    box = union_bounds(visit(child, (1, 0, 0, 1, 0, 0)) for child in root)
    if box is None:
        raise ValueError("generated artwork has no bounded geometry")
    return box


def crop_artwork(fragments, page_width, page_height):
    bounds = union_bounds(box for _, box in fragments)
    x, y, right, bottom = max(0., bounds[0]), max(0., bounds[1]), min(page_width, bounds[2]), min(page_height, bounds[3])
    if right <= x or bottom <= y:
        # Retain wholly off-page objects rather than silently discarding their source count.
        x, y, right, bottom = bounds
    width, height = right-x, bottom-y
    root = ET.Element(f"{{{SVG_NS}}}svg", {"width": str(width), "height": str(height),
                          "viewBox": f"{x} {y} {width} {height}"})
    for svg, _ in fragments:
        root.extend(ET.fromstring(svg))
    return ET.tostring(root, encoding="utf8"), (x, y, width, height)


def nearby_artwork(group, box):
    """Cap extent to stop page-spanning chains of nearby marks."""
    bounds = union_bounds([b for _, b in group] + [box])
    if len(group) >= 128 or bounds[2]-bounds[0] > 180 or bounds[3]-bounds[1] > 180:
        return False
    return any(math.hypot(max(0, b[0]-box[2], box[0]-b[2]),
                          max(0, b[1]-box[3], box[1]-b[3])) <= 12 for _, b in group)


def cluster_artwork(fragments):
    """Move a mark into a nearby earlier group only across disjoint intervening artwork."""
    groups = []
    for fragment in fragments:
        box = fragment[1]
        for index in range(len(groups)-1, -1, -1):
            group = groups[index]
            if not nearby_artwork(group, box):
                continue
            intervening = (b for other in groups[index+1:] for _, b in other)
            if any(box[0] <= b[2] and b[0] <= box[2] and box[1] <= b[3] and b[1] <= box[3]
                   for b in intervening):
                continue
            group.append(fragment)
            break
        else:
            groups.append([fragment])
    return groups


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
        shapes = []
        derived = []
        pending = []
        pending_reason = ""
        pending_bytes = 0

        def next_z():
            return len(ink)+len(images)+len(shapes)+len(derived)

        def render(strokes=(), shapes=(), image_elements=()):
            view = replace(p, strokes=list(strokes), shapes=list(shapes), image_elements=tuple(image_elements),
                           elements=(), sticky_notes=(), text_fragments=[], text_elements=())
            svg = page_to_svg(view, doc)
            if len(svg.encode("utf8")) > 20*MIB:
                raise ValueError("individual vector artwork exceeds byte limit")
            return svg

        def flush_artwork():
            nonlocal pending_bytes
            if not pending:
                return
            for group in cluster_artwork(pending):
                data, (x, y, w, h) = crop_artwork(group, width, height)
                filename = f"page-{p.index+1:04}-derived-{len(derived):04}.svg"
                emit(filename, data, 20*MIB)
                derived.append({"file": filename, "z": next_z(), "x": x, "y": y, "width": w, "height": h,
                                "reason": pending_reason, "source_count": len(group)})
            pending.clear()
            pending_bytes = 0

        def artwork(svg, reason):
            nonlocal pending_reason, pending_bytes
            box = svg_bounds(svg)
            size = len(svg.encode("utf8"))
            if pending and (reason != pending_reason or pending_bytes+size > 20*MIB):
                flush_artwork()
            pending_reason = reason
            pending.append((svg, box))
            pending_bytes += size

        for image in p.image_elements:
            member = "attachments/" + image.attachment_uuid
            filename = asset(doc, member)
            if filename.endswith(".pdf"):
                raise ValueError("PDF stickers are not supported yet")
            cropped = image.orig_width > 0 and image.orig_height > 0 and (
                abs(image.width-image.orig_width) > .1 or abs(image.height-image.orig_height) > .1)
            if cropped or abs(image.rotation_rad) > .00001:
                # Images remain individually selectable, even when their rectangles overlap.
                flush_artwork()
                artwork(render(image_elements=[image]), "cropped/rotated image")
                flush_artwork()
            else:
                flush_artwork()
                images.append({"file": filename, "x": image.x*SCALE, "y": image.y*SCALE,
                               "width": image.width*SCALE, "height": image.height*SCALE,
                               "z": next_z()})

        for s in p.strokes:
            if s.eraser_cuts or s.is_cut_start or s.is_cut_end:
                raise ValueError(f"page {p.index+1}: partially erased ink needs fidelity validation")
            if s.dash_pattern:
                artwork(render(strokes=[s]), "dashed ink")
                continue
            if s.native_cgpaths or s.outline_polygons or s.ribbon_path:
                artwork(render(strokes=[s]), "native outline ink")
                continue
            radii = [pt.pressure*SCALE for pt in s.points]
            widths = [2*r if r > 0 else s.width*SCALE for r in radii]
            if not widths or min(widths) <= 0 or max(widths) > 4*min(widths):
                artwork(render(strokes=[s]), "ink width outside native pressure range")
                continue
            flush_artwork()
            nominal = max(widths)
            pressure = max(widths)-min(widths) > .000001
            ink.append({"color": s.color_hex, "opacity": s.alpha, "width": nominal,
                        "highlighter": s.is_highlighter, "pressure": pressure,
                        "z": next_z(),
                        "points": [[pt.x*SCALE, pt.y*SCALE, (w/nominal-.25)/.75 if pressure else 1]
                                   for pt, w in zip(s.points, widths)]})
        flush_artwork()
        for shape in p.shapes:
            svg = render(shapes=[shape])
            native = native_shape(shape, svg)
            if native is None:
                artwork(svg, "shape geometry/style outside native model")
            else:
                flush_artwork()
                native["z"] = next_z()
                shapes.append(native)
        flush_artwork()
        data = {"width": width, "height": height, "background": background,
                "pdf_page": p.pdf_page_index, "ink": ink, "images": images, "shapes": shapes,
                "derived": derived, "warnings": warnings, "source_uuid": p.uuid,
                "source_strokes": len(p.strokes), "source_shapes": len(p.shapes),
                "source_images": len(p.image_elements)}
        filename = f"page-{p.index+1:04}.json"
        emit(filename, json.dumps(data, allow_nan=False, separators=(",", ":")).encode("utf8"), 64*MIB)
        pages.append(filename)
        print(f"Decoded page {p.index+1}: {len(ink)} editable strokes, {len(shapes)} editable shapes, {len(derived)} vector groups", flush=True)
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
    result = {"version": 2, "decoder": PIN, "schema": schema, "pages": pages,
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
