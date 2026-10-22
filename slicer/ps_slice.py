#!/usr/bin/env python3
"""The shop's headless slice, run inside the ps-slicer container (Dockerfile beside this file).

Operator, 2026-10-18: "after we evaluate it we automatically slice it based on the material they want and the size and
then send it through to the printers ... if it's just a simple print we can shoot it through with our favorite settings
for best quality and successful print". The site turns a member's checked mesh into an upload with a slice spec; the
hub fetches it, writes /job/job.json and /job/<mesh>, and runs this through /opt/ps-slicer/run-slice (no network, read
only, capped). Output: /job/out/sliced.3mf, a Bambu sliced plate the hub reads like any member's upload, and
/job/out/result.json.

job.json:
  {"mesh": "part.stl", "model": "A1 Mini" | "P2S" | "X1C",
   "filament": {"trayInfoIdx": "GFA00" | null, "type": "PLA", "colorHex": "#F2754E"},
   "fixes": {"orient": true, "brim": false, "supports": false}, "scale": 1.0}
  All three fixes are required booleans. scale is the site's one factor (the file's units and the member's scale); this
  wrapper applies it to the mesh (scale_stl), never OrcaSlicer.
  {"probe": true} instead asks what this image slices (caps()); every result.json carries the same answer.

Presets: Bambu's own system presets bundled with OrcaSlicer, resolved through their `inherits` chains here (the CLI does
not load the vendor bundle, so an unresolved preset silently loses its density, filament id and process values; checked
2026-10-18), marked `from: system` (the CLI refuses a machine preset from a user), then the shop's own settings
(SHOP_PROCESS) and the member's choices, each written out in both states (shop_process()). The model code Bambu writes into slice_info (N1, N7, BL-P001) comes from the
vendor's printer definition: the CLI leaves it blank, and the hub refuses a plate without it.

Stdlib only. `python3 ps_slice.py selftest` checks the pure parts without the slicer.
"""
from __future__ import annotations

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
# its process and filament presets carry. H2D waits: two nozzles need an extruder choice the spec does not carry yet.
MODELS = {
    "A1 Mini": ("Bambu Lab A1 mini 0.4 nozzle", "A1M"),
    "P2S": ("Bambu Lab P2S 0.4 nozzle", "P2S"),
    "X1C": ("Bambu Lab X1 Carbon 0.4 nozzle", "X1C"),
}
PROCESS = "0.20mm Standard @BBL {sfx}"

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
    scale = raw.get("scale", 1.0)
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or not SCALE_MIN <= float(scale) <= SCALE_MAX:
        raise SliceError("the size change is outside what the slicer accepts")
    return {
        "mesh": mesh, "model": model, "type": ftype,
        "color": color if isinstance(color, str) and HEX_RE.match(color) else None,
        "tray": tray if isinstance(tray, str) and TRAY_RE.match(tray) else None,
        "stated": stated,
        "orient": fixes["orient"], "brim": fixes["brim"], "supports": fixes["supports"],
        "scale": float(scale),
    }


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
    out: dict = {"scaleMin": SCALE_MIN, "scaleMax": SCALE_MAX, "lineWidth": {}}
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
    grams = sum(float(g) for g in re.findall(r'<filament [^>]*used_g="([0-9.]+)"', info))
    seconds = int(float((re.search(r'key="prediction" value="([0-9.]+)"', info) or [0, "0"])[1]))
    support = (re.search(r'key="support_used" value="(\w+)"', info) or [0, "false"])[1] == "true"
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
    filament_name = pick_filament(filaments, machine_name, sfx, spec["type"], spec["tray"], spec.get("stated"))
    filament = flatten(filaments, filament_name)
    if spec["color"]:
        filament["filament_colour"] = [spec["color"]]
    work = os.path.join(job_dir, "work")
    out = os.path.join(job_dir, "out")
    os.makedirs(work, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    paths = {}
    for kind, d in (("machine", machine), ("process", process), ("filament", filament)):
        paths[kind] = os.path.join(work, f"{kind}.json")
        with open(paths[kind], "w", encoding="utf-8") as fh:
            json.dump(d, fh)
    mesh = os.path.join(work, spec["mesh"])
    scale_stl(os.path.join(job_dir, spec["mesh"]), mesh, spec["scale"])
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
    for bad in ({**ok, "model": "H2D"}, {**ok, "mesh": "../x.stl"}, {**ok, "mesh": "x.exe"},
                {**ok, "filament": {"type": "wood"}}, {**ok, "scale": 5000}, {**ok, "scale": 0.0005}, {**ok, "scale": True},
                {**ok, "fixes": {"orient": True, "brim": False}}, {**ok, "fixes": {"orient": True, "brim": 1, "supports": False}}):
        try:
            read_spec(bad)
        except SliceError:
            continue
        raise AssertionError(f"accepted {bad}")
    assert read_spec({**ok, "filament": {"type": "PETG", "colorHex": "red", "trayInfoIdx": "x;"}})["color"] is None
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
    print("selftest ok")


def main(argv: list[str]) -> int:
    if argv[1:] == ["selftest"]:
        selftest()
        return 0
    out = os.path.join(JOB, "out")
    try:
        result = run()
    except (SliceError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        reason = str(exc) if isinstance(exc, SliceError) else "the slicer stopped before it finished"
        result = {"ok": False, "error": reason, **caps()}   # a refusal still says what this image takes
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    print(json.dumps(result))
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
