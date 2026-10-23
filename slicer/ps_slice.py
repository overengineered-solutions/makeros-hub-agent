#!/usr/bin/env python3
"""The shop's headless slice, run inside the ps-slicer container (Dockerfile beside this file).

Operator, 2026-10-18: "after we evaluate it we automatically slice it based on the material they want and the size and
then send it through to the printers ... if it's just a simple print we can shoot it through with our favorite settings
for best quality and successful print". The site turns a member's checked mesh into an upload with a slice spec; the
hub fetches it, writes /job/job.json and /job/<mesh>, and runs this through /opt/ps-slicer/run-slice (no network, read
only, capped). Output: /job/out/sliced.3mf, a Bambu sliced plate the hub reads like any member's upload, and
/job/out/result.json.

job.json:
  {"mesh": "part.stl", "model": "A1 Mini" | "P2S" | "X1C" | "H2D",
   "filament": {"trayInfoIdx": "GFA00" | null, "type": "PLA", "colorHex": "#F2754E"},
   "fixes": {"orient": true, "brim": false, "supports": false}, "scale": 1.0, "nozzle": "left" | "right" (the H2D only)}
  All three fixes are required booleans. scale is the site's one factor (the file's units and the member's scale); this
  wrapper applies it to the mesh (scale_stl), never OrcaSlicer. nozzle: on a two-nozzle printer, the one whose AMS holds the
  member's spool; the plate names it, and the hub dispatches on it (0.77).
  {"probe": true} instead asks what this image slices (caps()); every result.json carries the same answer.

Presets: Bambu's own system presets bundled with OrcaSlicer, resolved through their `inherits` chains here (the CLI does
not load the vendor bundle, so an unresolved preset silently loses its density, filament id and process values; checked
2026-10-18), marked `from: system` (the CLI refuses a machine preset from a user), then the shop's own settings
(SHOP_PROCESS) and the member's choices, each written out in both states (shop_process()). The model code Bambu writes into slice_info (N1, N7, BL-P001) comes from the
vendor's printer definition: the CLI leaves it blank, and the hub refuses a plate without it.

Validation is stdlib-only. Preparation uses the pinned container packages; `prepare-selftest` exercises their geometry.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import math
import json
import os
import re
import struct
import subprocess
import sys
import zipfile

ORCA = os.environ.get("PS_ORCA_BIN", "/opt/orca/bin/orca-slicer")
PROFILES = os.environ.get("PS_PROFILES", "/opt/orca/resources/profiles/BBL")
JOB = os.environ.get("PS_JOB_DIR", "/job")

# The shop's printer models the auto path slices for: the site's model name -> the Bambu machine preset and the suffix
# its process and filament presets carry. Every one is the 0.4 mm nozzle and the 0.20 mm process, always: operator, 2026-10-22,
# "we should never auto switch to a different nozzle size or layer height" (selftest checks both against the presets).
MODELS = {
    "A1 Mini": ("Bambu Lab A1 mini 0.4 nozzle", "A1M"),
    "P2S": ("Bambu Lab P2S 0.4 nozzle", "P2S"),
    "X1C": ("Bambu Lab X1 Carbon 0.4 nozzle", "X1C"),
    "H2D": ("Bambu Lab H2D 0.4 nozzle", "H2D"),   # board 459044ef: the spec names the nozzle (filament_map below)
}
PROCESS = "0.20mm Standard @BBL {sfx}"
# A two-nozzle printer's nozzles as the printer reports them (bambu_parse extruderId: 0 right, 1 left).
NOZZLES = {"right": 0, "left": 1}
TWO_NOZZLES = {"H2D"}
# Operator, 2026-10-22: "A1 minis PLA only" (an open printer; PETG and the rest go to the closed ones).
PLA_ONLY = {"A1 Mini"}

# The shop's own settings for every automatic slice, per model, in one place. Each value is written into the process, so a
# preset change or an OrcaSlicer default never decides it quietly. Change a model's line here and the hub's next release
# slices with it; the site's OrcaSlicer setup download should carry the same values (reports/auto-slice-quality-plan).
# sparse_infill_pattern: rectilinear (operator, 2026-10-22: "I like rectilinear"; board 6e8c967f). It lays one direction of
# lines per layer, turning 90 degrees each layer, so the nozzle never crosses lines it laid in the same layer; cubic, chosen
# first, lays three directions that cross in every layer, as grid does (nozzle scraping at speed). Bambu's presets give
# crosshatch, whose wavy transition layers look like gyroid ("the auto slicer defaults to gyroid which is overkill and takes
# longer", 2026-10-21). On the shop's four parts on the A1 mini at 15% (OrcaSlicer 2.4.2, 2026-10-21; the table is in pstation
# reports/auto-slice-quality-plan-2026-10-21.md) rectilinear sliced within 1% of cubic's time, 3 to 15% faster than crosshatch
# and 4 to 30% faster than gyroid, and 1 to 3.5% heavier than cubic. It is weaker between layers than a 3D pattern; a pull test
# on the bracket would measure what that costs. Not yet timed on the P2S and X1C.
SHOP_PROCESS: dict[str, dict[str, str]] = {
    "A1 Mini": {"sparse_infill_pattern": "rectilinear", "sparse_infill_density": "15%"},
    "P2S": {"sparse_infill_pattern": "rectilinear", "sparse_infill_density": "15%"},
    "X1C": {"sparse_infill_pattern": "rectilinear", "sparse_infill_density": "15%"},
    "H2D": {"sparse_infill_pattern": "rectilinear", "sparse_infill_density": "15%"},
}
# The factor the site sends (Send to PS step 2): the file's units and the member's scale in one number on the file as the site
# read it. 1:1000 of a file in millimetres up to a file in metres at full size; what fits is the bed's to say. Hubs before this
# took 0.05 to 20, and the site sends nothing outside that to a hub that reports an older version.
SCALE_MIN, SCALE_MAX = 0.001, 1000.0
# The process values that change what prints, reported with every slice (Send to PS step 2 review): the site binds the member's
# approval to them, so a job moved between boxes whose settings differ (cubic against rectilinear infill) asks the member again.
SETTINGS_KEYS = ("layer_height", "wall_loops", "sparse_infill_pattern", "sparse_infill_density", "top_shell_layers", "bottom_shell_layers")
TYPES = {"PLA", "PETG", "ABS", "ASA", "TPU"}
MESH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,118}\.(stl|obj|3mf)$", re.I)
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
TRAY_RE = re.compile(r"^[A-Z0-9]{3,8}$")


FIX_WORDS = {"orient": "turn the part onto its best side", "brim": "add a brim", "supports": "add supports"}


class SliceError(Exception):
    pass


# These imports stay inside prepare_model: install-slicer.py's local preflight is stdlib-only; member geometry runs only
# in the container, whose Dockerfile pins the permissive kernels (operator, 2026-10-23: "Have PStation fix it").
def prepare_model(src: str, dst: str, request: dict, min_wall: float, scale: float) -> dict:
    import base64
    import hashlib
    import io
    import math
    import numpy as np
    import networkx as nx
    import trimesh
    from manifold3d import Manifold, Mesh, OpType, CrossSection
    from PIL import Image, ImageDraw

    broken = "We could not repair this file safely. Export it again as a closed solid."
    surface = "Your model has no thickness. Give the walls a thickness in your program."
    tiny = "These are too small to print at this scale. Leave them off, or print a bigger scale."
    opening = "Thickening this wall would close the window beside it. Pick a bigger scale or fix it in your model."
    exact = "Thickening this wall would change a room size, the footprint or a connecting face. Pick a bigger scale or fix it in your model."
    changes, blocked = [], []
    original = trimesh.load_mesh(src, file_type="stl", process=False)
    if not 0 < len(original.faces) <= 300_000 or not np.isfinite(original.vertices).all():
        raise SliceError(broken)
    original.merge_vertices()
    original.apply_scale(scale)
    parts = list(original.split(only_watertight=False, repair=False))
    if not 0 < len(parts) <= 128:
        raise SliceError("There are too many separate pieces to fix safely. Send a smaller selection from your model.")

    def measure(meshes):
        mesh = trimesh.util.concatenate(meshes)
        return {"size": [round(float(x), 6) for x in mesh.extents],
                "volume": round(sum(abs(float(m.volume)) for m in meshes), 6), "parts": len(meshes)}

    # The same projection and scale in both pictures. Orange marks the affected surfaces, including material that was added.
    def picture(meshes, marked, bounds, basis, edges):
        corners = trimesh.bounds.corners(bounds) @ basis.T
        lo, hi = corners[:, :2].min(axis=0), corners[:, :2].max(axis=0)
        zoom = min(440 / max(hi[0] - lo[0], 0.01), 280 / max(hi[1] - lo[1], 0.01))
        im = Image.new("RGB", (480, 320), "#f8fafc")
        draw = ImageDraw.Draw(im)
        triangles = []
        for index, mesh in enumerate(meshes):
            pts = mesh.triangles @ basis.T
            for face, tri in enumerate(pts):
                xy = (tri[:, :2] - (hi + lo) / 2) * zoom + [240, 160]
                mark = index in marked and (marked[index] is None or face in marked[index])
                front = mesh.face_normals[face] @ basis[2] > 0
                triangles.append((float(tri[:, 2].mean()), [tuple(v) for v in xy], mark, front))
        for _, xy, mark, front in sorted(triangles, key=lambda t: t[0]):
            draw.polygon(xy, fill=("#f59e0b" if front else "#b45309") if mark else "#94a3b8", outline="#b45309" if mark else "#64748b")
        for i, boundary in edges.items():
            for edge in boundary:
                xy = ((meshes[i].vertices[edge] @ basis.T)[:, :2] - (hi + lo) / 2) * zoom + [240, 160]
                draw.line([tuple(v) for v in xy], fill="#f59e0b", width=3)
        out = io.BytesIO()
        im.save(out, format="PNG", optimize=True)
        return base64.b64encode(out.getvalue()).decode("ascii")

    def record(fix, words, before, after, unit, old, new, marked=None, default=True, face_marks=None, edges=None):
        bounds = np.array([np.minimum(trimesh.util.concatenate(old).bounds[0], trimesh.util.concatenate(new).bounds[0]),
                           np.maximum(trimesh.util.concatenate(old).bounds[1], trimesh.util.concatenate(new).bounds[1])])
        marks = {i: None for i in (range(max(len(old), len(new))) if marked is None else marked)}
        old_marks, new_marks = face_marks if face_marks is not None else (marks, marks)
        basis = np.array([[0.7071, -0.7071, 0], [0.4082, 0.4082, -0.8165], [0.5774, 0.5774, 0.5774]])
        if face_marks is not None:
            # Look at a repaired face, including one hidden from the usual shop view.
            i = next(iter(new_marks))
            normal = new[i].face_normals[next(iter(new_marks[i]))]
            up = np.array([0., 0., 1.]) if abs(normal[2]) < 0.9 else np.array([0., 1., 0.])
            right = np.cross(up, normal)
            right /= np.linalg.norm(right)
            view = normal + right * 0.35 + up * 0.25
            view /= np.linalg.norm(view)
            right = np.cross(up, view)
            right /= np.linalg.norm(right)
            basis = np.array([right, -np.cross(view, right), view])
        changes.append({"fix": fix, "words": words, "before": before, "after": after, "unit": unit, "default": default,
                        "beforeImage": picture(old, old_marks, bounds, basis, edges or {}), "afterImage": picture(new, new_marks, bounds, basis, {})})

    before = measure(parts)
    start = [p.copy() for p in parts]
    # A zero-height surface cannot be given an inside by a repair. Small holes: only planar triangles/quads <= 1 mm across.
    filled, hole_faces, hole_edges, inward, processed = 0, {}, {}, [], 0
    patched = [p.copy() for p in parts]
    for i, p in enumerate(parts):
        processed += 1
        if min(p.extents) < 1e-6:
            blocked.append(surface)
            continue
        edges = p.edges_sorted[trimesh.grouping.group_rows(p.edges_sorted, require_count=1)]
        if len(edges):
            graph = nx.from_edgelist(edges)
            loops = nx.cycle_basis(graph)
            if any(d != 2 for _, d in graph.degree) or not loops:
                blocked.append(broken)
                continue
            if any(len(loop) not in (3, 4) or np.linalg.norm(np.ptp(p.vertices[loop], axis=0)) > 1.0 or
                   np.linalg.svd(p.vertices[loop] - p.vertices[loop].mean(axis=0), compute_uv=False)[-1] > 0.01 for loop in loops):
                blocked.append(broken)
                continue
            if not trimesh.repair.fill_holes(p):
                blocked.append(broken)
                continue
            filled += len(loops)
            hole_faces[i] = set(range(len(start[i].faces), len(p.faces)))
            hole_edges[i] = edges
        patched[i] = p.copy()
        trimesh.repair.fix_winding(p)
        # fix_winding starts at an arbitrary triangle; keep the majority's direction before judging nested shells.
        turned = np.sum(np.einsum('ij,ij->i', patched[i].face_normals, p.face_normals) < 0)
        if turned * 2 == len(p.faces):
            blocked.append(broken)
        elif turned * 2 > len(p.faces):
            p.faces = p.faces[:, ::-1].copy()
        inward.append(p.volume < 0)
        # Assign faces so trimesh drops cached normals too; 5.1.1 invert() can keep the old normals.
        if p.volume < 0:
            p.faces = p.faces[:, ::-1].copy()
        if not p.is_volume:
            blocked.append(broken)
    if filled:
        record("holes", f"Filled {filled} small surface hole(s), each no more than 1 mm across.", filled, 0, "holes", start, patched,
               face_marks=({}, hole_faces), edges=hole_edges)

    def solid(p):
        m = Manifold(Mesh(np.asarray(p.vertices, dtype=np.float32), np.asarray(p.faces, dtype=np.uint32)))
        if m.status().name != "NoError" or m.is_empty():
            raise SliceError(broken)
        return m

    def mesh(m):
        data = m.to_mesh()
        return trimesh.Trimesh(np.array(data.vert_properties)[:, :3], np.array(data.tri_verts), process=False)

    # Containment defines the shell group; its outer shell sets the winding of the whole group.
    solids = []
    if not blocked:
        shells = [solid(p) for p in parts]
        parents = []
        for i, p in enumerate(parts):
            containers = [j for j, q in enumerate(parts) if i != j and np.all(q.bounds[0] <= p.bounds[0]) and
                          np.all(q.bounds[1] >= p.bounds[1]) and (shells[i] - shells[j]).volume() < 1e-6]
            if any(not (np.all(parts[j].bounds[0] < p.bounds[0]) and np.all(parts[j].bounds[1] > p.bounds[1])) for j in containers):
                raise SliceError(broken)   # touching shells do not prove a closed cavity
            parents.append(min(containers, key=lambda j: parts[j].volume) if containers else None)
            if any((shells[a] - shells[b]).volume() > 1e-6 and (shells[b] - shells[a]).volume() > 1e-6
                   for a in containers for b in containers):
                blocked.append(broken)
        depths = []
        for i in range(len(parts)):
            d, parent = 0, parents[i]
            while parent is not None:
                d, parent = d + 1, parents[parent]
            depths.append(d)
            if parents[i] is not None and inward[i] == inward[parents[i]]:
                blocked.append(broken)   # nested solids with no alternating winding do not prove a cavity
        if blocked:
            raise SliceError(broken)
        for i, p in enumerate(parts):
            if depths[i] % 2:
                continue
            s = solid(p)
            for j, parent in enumerate(parents):
                if parent == i:
                    s = s - solid(parts[j])
            solids.append(s)
        # A correctly inward cavity was never a flipped-face problem: put its winding back before measuring the repair.
        for i, p in enumerate(parts):
            if depths[i] % 2:
                p.faces = p.faces[:, ::-1].copy()
        face_marks = {i: set(np.flatnonzero(np.any(patched[i].faces != p.faces, axis=1))) for i, p in enumerate(parts)}
        face_marks = {i: faces for i, faces in face_marks.items() if faces}
        flipped = sum(len(faces) for faces in face_marks.values())
        if flipped:
            record("faces", f"Turned {flipped} inside-out or flipped face(s) the right way.", flipped, 0, "faces", patched, parts,
                   face_marks=(face_marks, face_marks))
        parts = [mesh(s) for s in solids]
        before["volume"] = round(sum(float(s.volume()) for s in solids), 6)

    if not blocked:
        old = [p.copy() for p in parts]
        bottom = min(float(p.bounds[0, 2]) for p in parts)
        if abs(bottom) > 1e-5:
            for p in parts:
                p.apply_translation([0, 0, -bottom])
            record("bed", f"Moved the whole model {abs(bottom):.3f} mm {'down' if bottom > 0 else 'up'} to the bed.", bottom, 0, "mm", old, parts)

        # First wall pilot: closed rectangular walls. No voxel wrap: it would round exact mating faces.
        # shortcut: curved, tapered and joined thin walls are refused; extend with protected-face data and tolerance coupons.
        old = [p.copy() for p in parts]
        footprint = CrossSection(Manifold.batch_boolean([solid(p) for p in parts], OpType.Add).project().to_polygons())
        for i, world in enumerate(old):
            vertical = world.face_normals[np.abs(world.face_normals[:, 2]) < 1e-6]
            normals = np.unique(np.round(vertical, 6), axis=0)
            normal = min(normals, key=lambda n: np.ptp(world.vertices @ n)) if 0 < len(normals) <= 32 else np.array([1., 0., 0.])
            normal = normal / np.linalg.norm(normal)
            frame = np.array([normal, [-normal[1], normal[0], 0], [0, 0, 1]])
            transform = np.eye(4)
            transform[:3, :3] = frame
            p = world.copy()
            p.apply_transform(transform)
            boxes = [np.array([(q.vertices @ frame.T).min(axis=0), (q.vertices @ frame.T).max(axis=0)]) for q in old]
            full = np.array([np.min([b[0] for b in boxes], axis=0), np.max([b[1] for b in boxes], axis=0)])
            axis = 0
            width = float(p.extents[axis])
            box = math.isclose(abs(p.volume), float(np.prod(p.extents)), rel_tol=1e-5)
            if not box:
                # Deterministic screening of every small component plus up to 3000 faces of a larger one.
                pick = np.linspace(0, len(p.faces) - 1, min(3000, len(p.faces)), dtype=int)
                points, normals = p.triangles_center[pick], p.face_normals[pick]
                origins = points - normals * 1e-5
                hits, rays, _ = p.ray.intersects_location(origins, -normals, multiple_hits=False)
                lengths = np.linalg.norm(hits - origins[rays], axis=1)
                horizontal = np.abs(normals[rays, 2]) > 0.7
                if np.any((lengths < 0.4 - 1e-4) & horizontal):
                    blocked.append("This floor is too thin to print. Give it at least 0.4 mm at print size in your model, or pick a bigger scale.")
                if np.any((lengths < min_wall - 1e-4) & ~horizontal):
                    blocked.append("We cannot thicken these walls while keeping their openings and connecting faces exact. Pick a bigger scale or fix them in your model.")
                continue
            if p.extents[2] <= width:
                if p.extents[2] < 0.4:
                    blocked.append("This floor is too thin to print. Give it at least 0.4 mm at print size in your model, or pick a bigger scale.")
                continue
            if width >= min_wall - 1e-5:
                continue
            if not request["thicken"]:
                blocked.append("This model still has thin walls. Choose wall thickening or pick a bigger scale.")
                continue
            if width < min_wall / 4:
                blocked.append(tiny)
                continue
            low = abs(p.bounds[0, axis] - full[0, axis]) < 1e-5
            high = abs(p.bounds[1, axis] - full[1, axis]) < 1e-5
            if low and high:
                blocked.append(exact)
                continue
            addition = min_wall - width
            bounds = p.bounds.copy()
            bounds[0, axis] -= 0 if low else addition if high else addition / 2
            bounds[1, axis] += 0 if high else addition if low else addition / 2
            candidate = trimesh.creation.box(bounds=bounds)
            # Any opposing part means a protected room/opening or mating surface. Do not narrow even a still-open gap.
            others = [j for j, b in enumerate(boxes) if j != i and all(
                min(p.bounds[1, k], b[1, k]) - max(p.bounds[0, k], b[0, k]) > 1e-5 for k in range(3) if k != axis)
                and (b[1, axis] <= p.bounds[0, axis] + 1e-5 or b[0, axis] >= p.bounds[1, axis] - 1e-5)]
            if others:
                closes = any(min(bounds[1, axis], boxes[j][1, axis]) - max(bounds[0, axis], boxes[j][0, axis]) > 1e-5 for j in others)
                blocked.append(opening if closes else exact)
                continue
            if any(j != i and min(p.bounds[1, 2], b[1, 2]) - max(p.bounds[0, 2], b[0, 2]) > 1e-5 and
                   min(p.bounds[1, 0], b[1, 0]) - max(p.bounds[0, 0], b[0, 0]) > 1e-5 and
                   (abs(p.bounds[1, 1] - b[0, 1]) < 1e-5 or abs(p.bounds[0, 1] - b[1, 1]) < 1e-5) for j, b in enumerate(boxes)):
                blocked.append(exact)
                continue
            if np.any(bounds[0, :2] < full[0, :2] - 1e-5) or np.any(bounds[1, :2] > full[1, :2] + 1e-5):
                blocked.append(exact)
                continue
            # Union, with a subtraction proof: not one bit of the original solid may be lost.
            candidate.apply_transform(np.linalg.inv(transform))
            a, b = solid(world), solid(candidate)
            grown = a + b
            if (CrossSection(grown.project().to_polygons()) - footprint).area() > 1e-4:   # float32 boolean round-trip, square mm
                blocked.append(exact)
                continue
            if (a - grown).volume() > 1e-6:
                raise SliceError(broken)
            was = [x.copy() for x in parts]
            parts[i] = mesh(grown)
            record("walls", f"Thickened wall {i + 1} from {width:.3f} to {min_wall:.3f} mm {'inward' if low or high else 'equally from its centre'}; added up to {addition if low or high else addition / 2:.3f} mm.",
                   width, min_wall, "mm", was, parts, [i])

        was = [p.copy() for p in parts]
        union = Manifold.batch_boolean([solid(p) for p in parts], OpType.Add)
        if union.status().name != "NoError":
            raise SliceError(broken)
        shells = [mesh(m) for m in union.decompose()]
        parts = [p for p in shells if p.volume > 0]
        for cavity in (p for p in shells if p.volume < 0):
            enclosing = [i for i, p in enumerate(parts) if np.all(p.bounds[0] < cavity.bounds[0]) and
                         np.all(p.bounds[1] > cavity.bounds[1]) and p.contains(cavity.vertices).all()]
            if not enclosing:
                raise SliceError(broken)
            i = min(enclosing, key=lambda j: parts[j].volume)
            parts[i] = trimesh.util.concatenate([parts[i], cavity])
        if len(parts) < len(was) or sum(abs(p.volume) for p in was) - union.volume() > 1e-4:
            record("merge", f"Merged overlapping or face-joined pieces: {len(was)} pieces became {len(parts)} solid(s).", len(was), len(parts), "parts", was, parts)

        # Cast down from bottom faces, never a bounding box: a roof above an opening must not be put on an imaginary floor.
        parts.sort(key=lambda p: float(p.bounds[0, 2]))
        for i, p in enumerate(parts):
            if p.bounds[0, 2] <= 1e-5:
                continue
            feet = p.vertices[np.isclose(p.vertices[:, 2], p.bounds[0, 2], atol=1e-5)]
            feet = np.vstack([feet, feet.mean(axis=0)])
            if len(feet) > 2000:
                blocked.append(broken)
                continue
            floors = np.zeros(len(feet))
            for j, q in enumerate(parts):
                if j == i or q.bounds[0, 2] >= p.bounds[0, 2]:
                    continue
                hits, rays, _ = q.ray.intersects_location(feet + [0, 0, 1e-5], np.tile([0, 0, -1], (len(feet), 1)), multiple_hits=False)
                for hit, ray in zip(hits, rays):
                    floors[ray] = max(floors[ray], hit[2])
            gap = float(p.bounds[0, 2] - floors.max())
            if gap <= 1e-5:
                continue
            if floors.min() <= 1e-5 or floors.max() - floors.min() > 0.01:
                blocked.append(f"Piece {i + 1} has no flat surface underneath it. Put it on the bed in your model, or ask at the desk.")
                continue
            if gap >= 2 and not request["moveLargeGaps"]:
                blocked.append(f"Piece {i + 1} is {gap:.3f} mm above what is below it. Choose to move pieces across gaps of 2 mm or more, or fix it in your model.")
                continue
            was = [x.copy() for x in parts]
            moved = p.copy()
            moved.apply_translation([0, 0, -gap])
            if any((solid(moved) ^ solid(q)).volume() > 1e-5 for j, q in enumerate(parts) if j != i):
                blocked.append(f"Moving piece {i + 1} would run into another part. Fix its position in your model.")
                continue
            parts[i] = moved
            record("lower", f"Moved piece {i + 1} down {gap:.3f} mm onto the flat surface below it.", gap, 0, "mm", was, parts, [i], default=gap < 2)

    result_mesh = trimesh.util.concatenate(parts)
    data = result_mesh.export(file_type="stl")
    result = {"version": 1, "checked": False, "inputSha256": hashlib.sha256(open(src, "rb").read()).hexdigest(), "sha256": hashlib.sha256(data).hexdigest(),
              "settings": {**request, "minWall": min_wall, "scale": scale}, "before": before, "after": measure(parts),
              "changes": changes, "blocked": list(dict.fromkeys(blocked))}
    if len(json.dumps(result)) > 450_000:
        raise SliceError("There are too many changes to show safely at once. Send a smaller selection from your model.")
    if not blocked:
        with open(dst, "wb") as fh:
            fh.write(data)
    print(f"prepare: processed={processed}, expected={len(start)}, changes={len(changes)}, blocked={len(result['blocked'])}", flush=True)
    return result


def read_spec(raw: dict) -> dict:
    """Validate job.json: an unknown model, a mesh name that is not a plain file name, an odd colour, a scale outside SCALE_MIN to
    SCALE_MAX are refused with a sentence a member can read."""
    if not isinstance(raw, dict):
        raise SliceError("the slice request is not readable")
    mesh, model = raw.get("mesh"), raw.get("model")
    if not isinstance(mesh, str) or not MESH_RE.match(mesh):
        raise SliceError("the file name is not one the slicer accepts")
    if model not in MODELS:
        raise SliceError(f"automatic slicing is not set up for the {model} yet")
    fil = raw.get("filament") or {}
    ftype = str(fil.get("type") or "").upper()
    if ftype not in TYPES:
        raise SliceError(f"automatic slicing does not know the material {fil.get('type')!r}")
    color = fil.get("colorHex")
    tray = fil.get("trayInfoIdx")
    # the spool's stated material as the printer reports it ("PLA", "PETG-CF"); absent from a site before 2026-10-19
    stated = str(fil.get("material") or "").strip().upper()[:24] or None
    fixes = raw.get("fixes") or {}
    # every Print it choice arrives as true or false (operator, 2026-10-21: an unticked brim still got OrcaSlicer's "auto"
    # brim); a request that leaves one out is refused rather than given a default nobody chose
    for k in ("orient", "brim", "supports"):
        if not isinstance(fixes.get(k), bool):
            raise SliceError(f"the slice request does not say whether to {FIX_WORDS[k]}")
    if model in PLA_ONLY and ftype != "PLA":
        raise SliceError(f"the {model} prints PLA only; pick a closed printer for {ftype}")
    nozzle = raw.get("nozzle")
    if model in TWO_NOZZLES and nozzle not in NOZZLES:
        raise SliceError(f"the slice request does not say which of the {model}'s nozzles holds the spool")
    scale = raw.get("scale", 1.0)
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or not SCALE_MIN <= float(scale) <= SCALE_MAX:
        raise SliceError("the size change is outside what the slicer accepts")
    prepare = raw.get("prepare")
    if prepare is not None and (not isinstance(prepare, dict) or set(prepare) != {"version", "moveLargeGaps", "thicken"} or
                                type(prepare["version"]) is not int or prepare["version"] != 1 or
                                any(type(prepare[k]) is not bool for k in ("moveLargeGaps", "thicken"))):
        raise SliceError("the model fix choices are not readable; check the file again")
    result = {
        "mesh": mesh, "model": model, "type": ftype,
        "color": color if isinstance(color, str) and HEX_RE.match(color) else None,
        "tray": tray if isinstance(tray, str) and TRAY_RE.match(tray) else None,
        "stated": stated,
        "orient": fixes["orient"], "brim": fixes["brim"], "supports": fixes["supports"],
        "scale": float(scale), "nozzle": nozzle if model in TWO_NOZZLES else None, "prepare": prepare,
    }
    if "colours" in raw:
        # Preparation can reorder or replace triangles, invalidating the checked colour ranges.
        if prepare is not None:
            raise SliceError("Model fixes need one colour. Pick one spool for all layers, or fix the model in your design program before using layer colours.")
        colours = raw["colours"]
        if not isinstance(colours, dict) or set(colours) != {"filaments", "parts"}:
            raise SliceError("the layer colours could not be read")
        fs, parts = colours["filaments"], colours["parts"]
        if not isinstance(fs, list) or not 2 <= len(fs) <= 64 or not isinstance(parts, list) or not 1 <= len(parts) <= 2000:
            raise SliceError("the layer colours are outside the slicer's limits")
        checked = []
        for f in fs:
            if not isinstance(f, dict) or not HEX_RE.fullmatch(str(f.get("colorHex", ""))):
                raise SliceError("a layer's spool colour is missing")
            child = read_spec({"mesh": mesh, "model": model, "filament": f, "nozzle": f.get("nozzle"), "fixes": fixes, "scale": scale})
            if child["type"] == "TPU" or not child["stated"]:
                raise SliceError("layer colours need one stated material that can change through the AMS")
            checked.append(child)
        if len({f["stated"] for f in checked}) != 1:
            raise SliceError("layer colours must all use the same material")
        end, used = 0, set()
        for part in parts:
            if not isinstance(part, dict) or any(type(part.get(k)) is not int for k in ("first", "count", "filament")):
                raise SliceError("a layer's triangle range could not be read")
            if part["first"] != end or part["count"] <= 0 or not 0 <= part["filament"] < len(fs):
                raise SliceError("the layer ranges do not cover the model exactly once")
            end += part["count"]
            used.add(part["filament"])
        if end > 300000 or used != set(range(len(fs))):
            raise SliceError("the layer ranges or spools do not match the model")
        result["colours"] = {"filaments": checked, "parts": parts}
    return result


def load_index(kind: str) -> dict[str, dict]:
    folder = os.path.join(PROFILES, kind)
    index: dict[str, dict] = {}
    for f in os.listdir(folder):
        if f.endswith(".json"):
            try:
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(d, dict):
                index[d.get("name", f[:-5])] = d
    return index


def flatten(index: dict[str, dict], name: str) -> dict:
    """Merge a preset with everything it inherits, child over parent, and mark it a system preset."""
    chain, seen, n = [], set(), name
    while n:
        if n in seen or n not in index:
            raise SliceError(f"the slicer's presets are missing {n!r}")
        seen.add(n)
        chain.append(index[n])
        n = index[n].get("inherits") or ""
    flat: dict = {}
    for d in reversed(chain):
        flat.update(d)
    flat["inherits"] = ""
    flat["from"] = "system"
    return flat


def pick_filament(index: dict[str, dict], machine: str, sfx: str, ftype: str, tray: str | None, stated: str | None = None) -> str:
    """The loaded spool's own preset for this printer (same Bambu filament id). Operator, 2026-10-19: "automatic slicing
    should absolutely use exact profiles of not only specialty spools but any we have information for", and "if we dont know
    what it is but it is stated as pla then its okay to go generic ... i mainly meant when we know what it is". So: a spool
    whose id has a preset for this printer always gets that preset. A spool the slicer does not know, stated as the plain
    material (PLA, PETG), gets the generic preset of that material. One stated as a variant (PETG-CF, PLA-CF) with no exact
    preset is refused: a carbon-fibre spool sliced as plain PETG is the failure this guards.

    A preset belongs to this printer when its name carries "@BBL <model>" OR its compatible_printers names the printer:
    Bambu's base printer, the X1 Carbon, has its presets without a suffix ("Generic PLA"), so a name-only search sliced the
    X1C's generic spools with Bambu PLA Basic's settings (2026-10-19 coverage check against OrcaSlicer 2.4.2)."""
    def compatible(d: dict) -> bool:
        cp = d.get("compatible_printers")
        return d.get("instantiation") == "true" and (not cp or machine in cp)
    candidates = []
    for name, d in index.items():
        cp = d.get("compatible_printers") or []
        mine = f"@BBL {sfx}" in name or machine in cp
        if not compatible(d) or not mine or "nozzle" in name and "0.4 nozzle" not in name:
            continue
        flat_id = None
        n, guard = name, 0
        while n and guard < 12 and flat_id is None:   # filament_id usually sits on the @base parent
            flat_id = index.get(n, {}).get("filament_id")
            n = index.get(n, {}).get("inherits") or ""
            guard += 1
        candidates.append((name, flat_id))
    if tray:
        exact = sorted(name for name, fid in candidates if fid == tray)
        if exact:
            # several presets can share an id (a "@BBL X1C" variant and the unsuffixed base); prefer the printer's own name
            return next((n for n in exact if f"@BBL {sfx}" in n), exact[0])
        if stated and stated != ftype:
            # the middle ground (operator, 2026-10-19: "some generic or bambu profiles for things like petg CF that might be
            # worth falling back to"): the same variant's generic or Bambu preset for this printer, never plain PETG for CF
            family = [n for n, _fid in candidates if n.upper().startswith((f"GENERIC {stated} ", f"BAMBU {stated} ")) or n.upper() in (f"GENERIC {stated}", f"BAMBU {stated}")]
            if family:
                return next((n for n in sorted(family) if n.startswith("Generic")), sorted(family)[0])
            raise SliceError(f"the shop has no slicer profile for this {stated} spool ({tray}) on this printer")
    for name, _fid in candidates:
        # the X1C's generic is plain "Generic PLA"; the others carry "@BBL <model>"
        if name.startswith(f"Generic {ftype} @BBL {sfx}") or name == f"Generic {ftype}":
            return name
    for name, _fid in candidates:
        if name.startswith(f"Bambu {ftype}") and "Basic" in name:
            return name
    for name, _fid in sorted(candidates):
        if name.startswith(f"Bambu {ftype} @BBL"):
            return name
    raise SliceError(f"there is no {ftype} preset for this printer")


def _first(v):
    return (v[0] if v else None) if isinstance(v, list) else v


def effective_settings(process: dict) -> dict[str, str]:
    """SETTINGS_KEYS as this slice used them: the preset after the shop's settings and the member's choices."""
    out = {}
    for k in SETTINGS_KEYS:
        v = _first(process.get(k))
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            out[k] = str(v)[:24]
    return out


def line_width(process: dict, machine: dict) -> float | None:
    """The outer wall's line width the process lays, in mm: a number, or a percent of the machine's nozzle."""
    raw, nozzle = _first(process.get("outer_wall_line_width") or process.get("line_width")), _first(machine.get("nozzle_diameter"))
    try:
        text = str(raw).strip()
        width = float(text[:-1]) / 100 * float(nozzle) if text.endswith("%") else float(text)
    except (TypeError, ValueError):
        return None
    return round(width, 3) if 0 < width <= 5 else None


def caps(processes: dict | None = None, machines: dict | None = None) -> dict:
    """What this image slices, for the hub to report to the site (Send to PS step 2): the scale range read_spec takes, and per
    model the outer wall line width of the process it slices with, so the site's minimum wall is two of this image's lines.
    Read from the image, so a box whose agent and image differ reports the image. Never raises: a preset that does not read
    leaves its model out."""
    out: dict = {"scaleMin": SCALE_MIN, "scaleMax": SCALE_MAX, "lineWidth": {}, "prepareVersion": 1, "colours": True}
    try:
        processes = load_index("process") if processes is None else processes
        machines = load_index("machine") if machines is None else machines
    except OSError:
        return out
    for model, (machine_name, sfx) in MODELS.items():
        try:
            width = line_width(flatten(processes, PROCESS.format(sfx=sfx)), flatten(machines, machine_name))
        except SliceError:
            continue
        if width:
            out["lineWidth"][model] = width
    return out


def shop_process(spec: dict) -> dict[str, str]:
    """The process values the shop sets on every automatic slice: its per-model settings, then each Print it choice in both
    states. Unticked means off, never OrcaSlicer's "auto" (its brim_type default is auto_brim, which the Bambu presets leave
    unset, so a part got a brim nobody asked for)."""
    out = dict(SHOP_PROCESS[spec["model"]])
    out.update({"brim_type": "outer_only", "brim_width": "5"} if spec["brim"] else {"brim_type": "no_brim"})
    out.update({"enable_support": "1", "support_type": "tree(auto)"} if spec["supports"] else {"enable_support": "0"})
    return out


def nozzle_map(machine: dict, nozzle: str) -> dict:
    """The member's one filament on the nozzle the spec names (board 459044ef: "put the member's filament on the nozzle whose AMS
    has that spool (filament_map manual), so hub 0.77's plate routing reads it"). OrcaSlicer's manual map names a logical extruder
    from 1; the machine preset's physical_extruder_map says which nozzle each is (the H2D's ["1", "0"]: extruder 1 is the left)."""
    try:
        physical = [int(v) for v in machine.get("physical_extruder_map") or []]
    except (TypeError, ValueError):
        physical = []
    if NOZZLES[nozzle] not in physical:
        raise SliceError("the slicer's presets do not say which nozzle is which")
    return {"filament_map_mode": "Manual", "filament_map": [str(physical.index(NOZZLES[nozzle]) + 1)]}


def orca_args(spec: dict, paths: dict[str, str], out: str, mesh: str) -> list[str]:
    """The OrcaSlicer command line, with orientation stated every time, not only when it differs from a default. No --scale:
    the mesh it is given is already at its printed size (scale_stl)."""
    return [ORCA, "--slice", "0", "--arrange", "1", "--orient", "1" if spec["orient"] else "0",
            "--load-settings", f"{paths['machine']};{paths['process']}", "--load-filaments", paths["filament"],
            "--outputdir", out, "--export-3mf", "sliced.3mf", mesh]


def scale_stl(src: str, dst: str, factor: float) -> None:
    """The member's mesh at its printed size: every vertex times the factor, once, written to dst (src, the hub's copy, stays as
    it came). OrcaSlicer 2.4.2's own --scale crashed on every factor but 1 (exit 139, a 20 mm cube at 2x) and checked the bed
    before scaling (a 20 m building at 1:150 "does not fit"), measured 2026-10-22 against the ps-slicer:2.4.2-1 image, which
    is why the hubs up to 0.79 could not slice any Print it size but 100%. The site sends its checked mesh as a binary STL."""
    with open(src, "rb") as fh:
        data = bytearray(fh.read())
    n = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else -1
    if factor != 1.0 and len(data) != 84 + 50 * n:
        raise SliceError("the shop's slicer can resize only the binary STL the site sends")
    if factor != 1.0:
        for i in range(n):
            at = 84 + 50 * i + 12   # past the facet normal, which a uniform scale leaves as it is
            struct.pack_into("<9f", data, at, *(c * factor for c in struct.unpack_from("<9f", data, at)))
    with open(dst, "wb") as fh:
        fh.write(data)


def colour_3mf(stl: str, dst: str, parts: list[dict]) -> None:
    """One assembled object with one volume per checked object: arrange/orient cannot separate the model's parts."""
    with open(stl, "rb") as fh:
        data = fh.read()
    n = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else -1
    if len(data) != 84 + n * 50 or sum(p["count"] for p in parts) != n:
        raise SliceError("the layer ranges do not match the checked STL")
    model = ET.Element("model", {"unit": "millimeter", "xml:lang": "en-US", "xmlns": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"})
    resources = ET.SubElement(model, "resources")
    config = ET.Element("config")
    parent_id = len(parts) + 1
    obj_settings = ET.SubElement(config, "object", {"id": str(parent_id)})
    ET.SubElement(obj_settings, "metadata", {"key": "name", "value": "Layer colours"})
    for i, part in enumerate(parts):
        obj = ET.SubElement(resources, "object", {"id": str(i + 1), "type": "model"})
        mesh = ET.SubElement(obj, "mesh")
        vertices = ET.SubElement(mesh, "vertices")
        triangles = ET.SubElement(mesh, "triangles")
        for t in range(part["count"]):
            coords = struct.unpack_from("<9f", data, 84 + (part["first"] + t) * 50 + 12)
            if not all(math.isfinite(c) for c in coords):
                raise SliceError("the checked model has a coordinate that is not finite")
            for v in range(3):
                ET.SubElement(vertices, "vertex", dict(zip(("x", "y", "z"), (format(c, ".9g") for c in coords[v * 3:v * 3 + 3]))))
            ET.SubElement(triangles, "triangle", {"v1": str(t * 3), "v2": str(t * 3 + 1), "v3": str(t * 3 + 2)})
        volume = ET.SubElement(obj_settings, "part", {"id": str(i + 1), "subtype": "normal_part"})
        ET.SubElement(volume, "metadata", {"key": "name", "value": "Part " + str(i + 1)})
        ET.SubElement(volume, "metadata", {"key": "extruder", "value": str(part["filament"] + 1)})
    parent = ET.SubElement(resources, "object", {"id": str(parent_id), "type": "model"})
    components = ET.SubElement(parent, "components")
    for i in range(len(parts)):
        ET.SubElement(components, "component", {"objectid": str(i + 1)})
    build = ET.SubElement(model, "build")
    ET.SubElement(build, "item", {"objectid": str(parent_id)})
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", ET.tostring(model, encoding="utf-8", xml_declaration=True))
        z.writestr("Metadata/model_settings.config", ET.tostring(config, encoding="utf-8", xml_declaration=True))
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')


def set_model_id(threemf: str, model_id: str) -> None:
    """Write the printer's model code into Metadata/slice_info.config, as Bambu Studio and OrcaSlicer's desktop app do."""
    tmp = threemf + ".tmp"
    with zipfile.ZipFile(threemf) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "Metadata/slice_info.config":
                text = data.decode("utf-8")
                text, n = re.subn(r'(<metadata key="printer_model_id" value=")[^"]*(")', r"\g<1>" + model_id + r"\g<2>", text)
                if n == 0:
                    raise SliceError("the slicer wrote a plate without the printer model field")
                data = text.encode("utf-8")
            zout.writestr(item, data)
    os.replace(tmp, threemf)


def summarize(threemf: str) -> dict:
    with zipfile.ZipFile(threemf) as z:
        names = z.namelist()
        if "Metadata/plate_1.gcode" not in names:
            raise SliceError("the slicer produced no print for plate 1")
        info = z.read("Metadata/slice_info.config").decode("utf-8")
        gcode = z.read("Metadata/plate_1.gcode")
    grams = sum(float(g) for g in re.findall(r'<filament [^>]*used_g="([0-9.]+)"', info))
    seconds = int(float((re.search(r'key="prediction" value="([0-9.]+)"', info) or [0, "0"])[1]))
    # supports the slice laid, from its G-code's Support features; slice_info's support_used is only the setting (OrcaSlicer
    # writes enable_support there, PrintApply.cpp), so a ticked box over a part that needed none read "with temporary supports"
    # on the member's confirm card (board 72f424f2: "show the member what the slice actually used")
    support = b"; FEATURE: Support" in gcode
    model_id = (re.search(r'key="printer_model_id" value="([^"]*)"', info) or [0, ""])[1]
    return {"grams": round(grams, 2), "seconds": seconds, "supportUsed": support, "modelId": model_id,
            "plates": sum(1 for n in names if re.fullmatch(r"Metadata/plate_\d+\.gcode", n))}


def run(job_dir: str | None = None) -> dict:
    job_dir = job_dir or JOB
    with open(os.path.join(job_dir, "job.json"), encoding="utf-8") as fh:
        raw = json.load(fh)
    if isinstance(raw, dict) and raw.get("probe") is True:
        return {"ok": True, "probe": True, **caps()}   # the hub asking what this image slices; nothing is sliced
    spec = read_spec(raw)
    machine_name, sfx = MODELS[spec["model"]]
    machines, processes, filaments = load_index("machine"), load_index("process"), load_index("filament")
    machine = flatten(machines, machine_name)
    vendor = machines.get(machine.get("printer_model", ""), {})
    model_id = vendor.get("model_id")
    if not model_id:
        raise SliceError("the slicer's presets do not name this printer's model code")
    process = flatten(processes, PROCESS.format(sfx=sfx))
    process.update(shop_process(spec))
    process["curr_bed_type"] = vendor.get("default_bed_type") or "Textured PEI Plate"
    if "colours" in spec:
        # Keep the tower inside the A1 bed, past the X1C exclusion and in both H2D nozzle areas; crowded plates still fail collision checks.
        process.update({"wipe_tower_x": ["40" if spec["model"] in {"X1C", "H2D"} else "15"], "wipe_tower_y": ["15"]})
    selected = spec["colours"]["filaments"] if "colours" in spec else [spec]
    if spec.get("nozzle"):
        process.update({"filament_map_mode": "Manual", "filament_map": [nozzle_map(machine, f["nozzle"])["filament_map"][0] for f in selected]})
    filament_names, presets = [], []
    for f in selected:
        name = pick_filament(filaments, machine_name, sfx, f["type"], f["tray"], f.get("stated"))
        preset = flatten(filaments, name)
        if f["color"]:
            preset["filament_colour"] = [f["color"]]
        filament_names.append(name)
        presets.append(preset)
    filament_name = "; ".join(filament_names)
    work = os.path.join(job_dir, "work")
    out = os.path.join(job_dir, "out")
    os.makedirs(work, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    paths = {}
    for kind, d in [("machine", machine), ("process", process)] + [("filament" + str(i), f) for i, f in enumerate(presets)]:
        paths[kind] = os.path.join(work, f"{kind}.json")
        with open(paths[kind], "w", encoding="utf-8") as fh:
            json.dump(d, fh)
    paths["filament"] = ";".join(paths["filament" + str(i)] for i in range(len(presets)))
    mesh = os.path.join(work, spec["mesh"])
    prepared = None
    if spec["prepare"]:
        width = line_width(process, machine)
        if width is None:
            raise SliceError("the printer's extrusion width is unknown, so its walls cannot be fixed safely")
        prepared = prepare_model(os.path.join(job_dir, spec["mesh"]), mesh, spec["prepare"], width * 2, spec["scale"])
        # Persist the offer even when the geometry or slicer refuses it; no refused mesh goes to Orca.
        with open(os.path.join(out, "prepare.json"), "w", encoding="utf-8") as fh:
            json.dump(prepared, fh)
        if prepared["blocked"]:
            raise SliceError(" ".join(prepared["blocked"]))
    else:
        scale_stl(os.path.join(job_dir, spec["mesh"]), mesh, spec["scale"])
    if "colours" in spec:
        assembled = os.path.join(work, "colours.3mf")
        colour_3mf(mesh, assembled, spec["colours"]["parts"])
        mesh = assembled
    args = orca_args(spec, paths, out, mesh)
    p = subprocess.run(args, capture_output=True, text=True, timeout=540, env={**os.environ, "HOME": "/tmp"})
    sliced = os.path.join(out, "sliced.3mf")
    if p.returncode != 0 or not os.path.exists(sliced):
        code = re.search(r"return (-?\d+)", p.stdout + p.stderr)
        if code and code.group(1) == "-50":   # OrcaSlicer: an object outside the plate after arranging
            raise SliceError("the part does not fit on this printer's bed; choose a larger printer or scale it down")
        tail = (p.stdout + p.stderr).strip().splitlines()[-3:]
        raise SliceError("the slicer could not slice this part" + (f" ({' / '.join(tail)[:300]})" if tail else ""))
    set_model_id(sliced, model_id)
    result = {"ok": True, "filament": filament_name, "process": PROCESS.format(sfx=sfx), "settings": effective_settings(process),
              **summarize(sliced), **caps(processes, machines)}
    if prepared:
        result["prepare"] = prepared
        # Orca 2.4.2 reports these as text in result.json. A prepared file with disappearing or floating regions is not approved.
        log_path = os.path.join(out, "result.json")
        if os.path.exists(log_path):
            warnings = json.dumps(json.load(open(log_path, encoding="utf-8"))).lower()
            if any(w in warnings for w in ("floating regions", "floating cantilever", "empty layers", "too thin")):
                raise SliceError("The slice still has missing or floating parts. Pick a bigger scale or fix it in your model.")
    if result["plates"] != 1:
        raise SliceError(f"the part does not fit one plate (the slicer made {result['plates']}); scale it down or split it")
    if result["grams"] <= 0:
        raise SliceError("the slicer reported no filament for this part")
    return result


def selftest() -> None:
    ok = {"mesh": "part.stl", "model": "A1 Mini", "filament": {"type": "pla", "colorHex": "#F2754E", "trayInfoIdx": "GFA00"},
          "fixes": {"orient": True, "brim": False, "supports": False}}
    s = read_spec(ok)
    assert set(SHOP_PROCESS) == set(MODELS), "every model the auto path slices for needs its shop settings"
    assert shop_process(s)["brim_type"] == "no_brim" and shop_process(s)["enable_support"] == "0", shop_process(s)
    assert s["type"] == "PLA" and s["color"] == "#F2754E" and s["tray"] == "GFA00" and s["orient"] and not s["brim"], s
    assert read_spec({**ok, "model": "H2D", "nozzle": "left"})["nozzle"] == "left" and s["nozzle"] is None
    assert nozzle_map({"physical_extruder_map": ["1", "0"]}, "left") == {"filament_map_mode": "Manual", "filament_map": ["1"]}
    assert nozzle_map({"physical_extruder_map": ["1", "0"]}, "right")["filament_map"] == ["2"]
    for bad in ({**ok, "model": "H2D"}, {**ok, "model": "H2D", "nozzle": "middle"}, {**ok, "mesh": "../x.stl"}, {**ok, "mesh": "x.exe"},
                {**ok, "filament": {"type": "wood"}}, {**ok, "filament": {"type": "PETG"}}, {**ok, "scale": 5000}, {**ok, "scale": 0.0005}, {**ok, "scale": True},
                {**ok, "fixes": {"orient": True, "brim": False}}, {**ok, "fixes": {"orient": True, "brim": 1, "supports": False}}):
        try:
            read_spec(bad)
        except SliceError:
            continue
        raise AssertionError(f"accepted {bad}")
    assert read_spec({**ok, "model": "P2S", "filament": {"type": "PETG", "colorHex": "red", "trayInfoIdx": "x;"}})["color"] is None
    idx = {"a": {"name": "a", "inherits": "b", "k": 1}, "b": {"name": "b", "k": 0, "j": 2}}
    assert flatten(idx, "a") == {"name": "a", "inherits": "", "k": 1, "j": 2, "from": "system"}
    try:
        flatten({"a": {"inherits": "a"}}, "a")
        raise AssertionError("a loop was accepted")
    except SliceError:
        pass
    assert effective_settings({"wall_loops": "2", "sparse_infill_pattern": "rectilinear", "layer_height": ["0.2"], "x": "y"}) == \
        {"wall_loops": "2", "sparse_infill_pattern": "rectilinear", "layer_height": "0.2"}
    assert line_width({"outer_wall_line_width": "0.42"}, {}) == 0.42
    assert line_width({"outer_wall_line_width": "105%"}, {"nozzle_diameter": ["0.4"]}) == 0.42
    assert line_width({"outer_wall_line_width": "0"}, {}) is None and line_width({}, {}) is None
    if os.path.isdir(PROFILES):   # inside the image (its build runs this): every model's process names its line width
        got = caps()
        assert set(got["lineWidth"]) == set(MODELS) and (got["scaleMin"], got["scaleMax"]) == (SCALE_MIN, SCALE_MAX), got
        # the 0.4 mm nozzle and the 0.20 mm layer on every model, whatever a preset update brings (operator, 2026-10-22: "we should
        # never auto switch to a different nozzle size or layer height"), and each nozzle of a two-nozzle printer its own extruder
        machines, processes = load_index("machine"), load_index("process")
        for model, (machine_name, sfx) in MODELS.items():
            m, p = flatten(machines, machine_name), flatten(processes, PROCESS.format(sfx=sfx))
            assert set(m["nozzle_diameter"]) == {"0.4"} and str(_first(p["layer_height"])) == "0.2", model
            if model in TWO_NOZZLES:
                assert nozzle_map(m, "left") != nozzle_map(m, "right"), model
    print("selftest ok")


def main(argv: list[str]) -> int:
    if argv[1:] == ["selftest"]:
        selftest()
        return 0
    if argv[1:] == ["prepare-selftest"]:
        import tempfile
        import trimesh
        with tempfile.TemporaryDirectory() as root:
            src, dst = os.path.join(root, "in.stl"), os.path.join(root, "fixed.stl")
            base = trimesh.creation.box(bounds=[[0, 0, 3], [20, 20, 5]])
            wall = trimesh.creation.box(bounds=[[0, 0, 5], [0.5, 20, 15]])
            trimesh.util.concatenate([base, wall]).export(src)
            got = prepare_model(src, dst, {"version": 1, "thicken": True, "moveLargeGaps": False}, 0.84, 1)
            assert not got["blocked"] and {c["fix"] for c in got["changes"]} >= {"walls", "bed", "merge"}, got["blocked"]
            after = trimesh.load_mesh(dst)
            assert after.is_volume and after.bounds[0, 2] == 0 and after.volume > base.volume + wall.volume
        print("prepare selftest ok")
        return 0
    out = os.path.join(JOB, "out")
    try:
        result = run()
    except (SliceError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        reason = str(exc) if isinstance(exc, SliceError) else "the slicer stopped before it finished"
        result = {"ok": False, "error": reason, **caps()}   # a refusal still says what this image takes
        prepare_path = os.path.join(out, "prepare.json")
        if os.path.exists(prepare_path):
            result["prepare"] = json.load(open(prepare_path, encoding="utf-8"))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    print(json.dumps({k: v for k, v in result.items() if k != "prepare"}))
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
