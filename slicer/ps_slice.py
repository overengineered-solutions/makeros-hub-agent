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

Presets: Bambu's own system presets bundled with OrcaSlicer, resolved through their `inherits` chains here (the CLI does
not load the vendor bundle, so an unresolved preset silently loses its density, filament id and process values; checked
2026-10-18), marked `from: system` (the CLI refuses a machine preset from a user), then the shop's overrides: the
textured PEI plate, a brim, tree supports. The model code Bambu writes into slice_info (N1, N7, BL-P001) comes from the
vendor's printer definition: the CLI leaves it blank, and the hub refuses a plate without it.

Stdlib only. `python3 ps_slice.py selftest` checks the pure parts without the slicer.
"""
from __future__ import annotations

import json
import os
import re
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
TYPES = {"PLA", "PETG", "ABS", "ASA", "TPU"}
MESH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,118}\.(stl|obj|3mf)$", re.I)
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
TRAY_RE = re.compile(r"^[A-Z0-9]{3,8}$")


class SliceError(Exception):
    pass


def read_spec(raw: dict) -> dict:
    """Validate job.json: an unknown model, a mesh name that is not a plain file name, an odd colour, a scale outside 0.05 to 20
    are refused with a sentence a member can read."""
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
    fixes = raw.get("fixes") or {}
    scale = raw.get("scale", 1.0)
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or not 0.05 <= float(scale) <= 20:
        raise SliceError("the size change is outside what the slicer accepts")
    return {
        "mesh": mesh, "model": model, "type": ftype,
        "color": color if isinstance(color, str) and HEX_RE.match(color) else None,
        "tray": tray if isinstance(tray, str) and TRAY_RE.match(tray) else None,
        "orient": bool(fixes.get("orient", True)), "brim": bool(fixes.get("brim")), "supports": bool(fixes.get("supports")),
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


def pick_filament(index: dict[str, dict], machine: str, sfx: str, ftype: str, tray: str | None) -> str:
    """The loaded spool's own preset when there is one for this printer (same Bambu filament id), else the generic one."""
    def compatible(d: dict) -> bool:
        cp = d.get("compatible_printers")
        return d.get("instantiation") == "true" and (not cp or machine in cp)
    candidates = []
    for name, d in index.items():
        if not compatible(d) or f"@BBL {sfx}" not in name or "nozzle" in name and "0.4 nozzle" not in name:
            continue
        flat_id = None
        n, guard = name, 0
        while n and guard < 12 and flat_id is None:   # filament_id usually sits on the @base parent
            flat_id = index.get(n, {}).get("filament_id")
            n = index.get(n, {}).get("inherits") or ""
            guard += 1
        candidates.append((name, flat_id))
    if tray:
        for name, fid in candidates:
            if fid == tray:
                return name
    for name, _fid in candidates:
        if name.startswith(f"Generic {ftype} @BBL {sfx}"):
            return name
    for name, _fid in candidates:
        if name.startswith(f"Bambu {ftype}") and "Basic" in name:
            return name
    for name, _fid in sorted(candidates):
        if name.startswith(f"Bambu {ftype} @BBL"):
            return name
    raise SliceError(f"there is no {ftype} preset for this printer")


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


def run(job_dir: str = JOB) -> dict:
    with open(os.path.join(job_dir, "job.json"), encoding="utf-8") as fh:
        spec = read_spec(json.load(fh))
    machine_name, sfx = MODELS[spec["model"]]
    machines, processes, filaments = load_index("machine"), load_index("process"), load_index("filament")
    machine = flatten(machines, machine_name)
    vendor = machines.get(machine.get("printer_model", ""), {})
    model_id = vendor.get("model_id")
    if not model_id:
        raise SliceError("the slicer's presets do not name this printer's model code")
    process = flatten(processes, PROCESS.format(sfx=sfx))
    process["curr_bed_type"] = vendor.get("default_bed_type") or "Textured PEI Plate"
    if spec["brim"]:
        process.update({"brim_type": "outer_only", "brim_width": "5"})
    if spec["supports"]:
        process.update({"enable_support": "1", "support_type": "tree(auto)"})
    filament_name = pick_filament(filaments, machine_name, sfx, spec["type"], spec["tray"])
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
    args = [ORCA, "--slice", "0", "--arrange", "1", "--orient", "1" if spec["orient"] else "0",
            "--load-settings", f"{paths['machine']};{paths['process']}", "--load-filaments", paths["filament"],
            "--outputdir", out, "--export-3mf", "sliced.3mf"]
    if spec["scale"] != 1.0:
        args += ["--scale", f"{spec['scale']:.4f}"]
    args.append(os.path.join(job_dir, spec["mesh"]))
    p = subprocess.run(args, capture_output=True, text=True, timeout=540, env={**os.environ, "HOME": "/tmp"})
    sliced = os.path.join(out, "sliced.3mf")
    if p.returncode != 0 or not os.path.exists(sliced):
        code = re.search(r"return (-?\d+)", p.stdout + p.stderr)
        if code and code.group(1) == "-50":   # OrcaSlicer: an object outside the plate after arranging
            raise SliceError("the part does not fit on this printer's bed; choose a larger printer or scale it down")
        tail = (p.stdout + p.stderr).strip().splitlines()[-3:]
        raise SliceError("the slicer could not slice this part" + (f" ({' / '.join(tail)[:300]})" if tail else ""))
    set_model_id(sliced, model_id)
    result = {"ok": True, "filament": filament_name, "process": PROCESS.format(sfx=sfx), **summarize(sliced)}
    if result["plates"] != 1:
        raise SliceError(f"the part does not fit one plate (the slicer made {result['plates']}); scale it down or split it")
    if result["grams"] <= 0:
        raise SliceError("the slicer reported no filament for this part")
    return result


def selftest() -> None:
    ok = {"mesh": "part.stl", "model": "A1 Mini", "filament": {"type": "pla", "colorHex": "#F2754E", "trayInfoIdx": "GFA00"}}
    s = read_spec(ok)
    assert s["type"] == "PLA" and s["color"] == "#F2754E" and s["tray"] == "GFA00" and s["orient"] and not s["brim"], s
    for bad in ({**ok, "model": "H2D"}, {**ok, "mesh": "../x.stl"}, {**ok, "mesh": "x.exe"},
                {**ok, "filament": {"type": "wood"}}, {**ok, "scale": 50}, {**ok, "scale": True}):
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
        result = {"ok": False, "error": reason}
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    print(json.dumps(result))
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
