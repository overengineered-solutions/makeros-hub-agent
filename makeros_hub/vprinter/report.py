from __future__ import annotations

import colorsys
import hashlib
from typing import Any


def _derive_hex(serial: str, tag: str, idx: int, length: int, upper: bool) -> str:
    """Deterministic hex id from (serial, tag, idx) — STABLE across heartbeats
    and restarts (no randomness), so an AMS's advertised hardware identity never
    churns. sha256 keeps it collision-free across units/modules."""
    digest = hashlib.sha256(f"{serial}:{tag}:{idx}".encode("utf-8")).hexdigest()[:length]
    return digest.upper() if upper else digest


def ams_unit_serial(serial: str, idx: int) -> str:
    """The AMS unit's hardware id — shaped like a real ams_id (15 uppercase hex,
    e.g. '19C06A541800179'). The SAME value is the get_version AMS module's `sn`
    AND the push_status unit's `ams_id`; that module<->unit linkage is what lets
    OrcaSlicer's Device tab resolve a GENERIC spool (recognized ids self-resolve
    off their own id; a generic needs the AMS itself fully identified)."""
    return _derive_hex(serial, "ams", idx, 15, upper=True)


def ams_chip_id(serial: str, idx: int) -> str:
    """The AMS unit's chip id — shaped like a real chip_id (31 lowercase hex,
    e.g. '6dfc3b4d8444300d47323931fffffff')."""
    return _derive_hex(serial, "chip", idx, 31, upper=False)


def _module_serial(serial: str, name: str) -> str:
    """A per-module serial for the modules a real printer gives their OWN sn
    (mc, th) rather than the mainboard serial (ota, esp32 share the mainboard's)."""
    return _derive_hex(serial, f"mod:{name}", 0, 15, upper=True)


MATERIALS = [
    ("PLA", "GFA00", "Generic PLA", "190", "230"),
    ("PETG", "GFG00", "Generic PETG", "230", "260"),
    ("ABS", "GFB00", "Generic ABS", "240", "270"),
    ("TPU", "GFU00", "Generic TPU", "210", "240"),
    ("ASA", "GFA01", "Generic ASA", "240", "270"),
    ("PA", "GFN00", "Generic PA", "260", "290"),
    ("PC", "GFC00", "Generic PC", "260", "300"),
    ("PLA-CF", "GFA11", "Generic PLA-CF", "200", "240"),
]


def build_push_status(
    units: int = 4,
    trays: int = 4,
    sequence_id: int | str = 1,
    filaments: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    gcode_state: str = "IDLE",
    gcode_file: str = "",
    prepare_percent: str = "0",
    ams_version: int = 4,
    serial: str = "",
) -> dict[str, Any]:
    if units <= 0:
        raise ValueError("units must be positive")
    if trays <= 0:
        raise ValueError("trays must be positive")

    total_trays = units * trays
    colors = generate_colors(total_trays) if filaments is None else []
    ams_units = []
    filled_indices: list[int] = []  # global slot indices that carry filament
    for unit_idx in range(units):
        tray_items = []
        for tray_idx in range(trays):
            global_idx = unit_idx * trays + tray_idx
            if filaments is None:
                tray = _synthetic_tray(global_idx, tray_idx, colors[global_idx])
                filled_indices.append(global_idx)
            elif global_idx < len(filaments):
                tray = _coerce_pool_tray(filaments[global_idx], tray_idx)
                filled_indices.append(global_idx)
            else:
                tray = _empty_tray(tray_idx)
            tray_items.append(tray)
        ams_units.append(
            {
                "id": str(unit_idx),
                # Hardware identity a real AMS unit always carries. `ams_id` equals
                # this unit's get_version AMS module `sn` (ams_unit_serial), giving
                # OrcaSlicer the module<->unit linkage it needs to resolve a GENERIC
                # spool in the Device tab. Stable per (serial, unit) so it never
                # churns ams.version.
                "ams_id": ams_unit_serial(serial, unit_idx),
                "chip_id": ams_chip_id(serial, unit_idx),
                "info": "2003",
                "temp": "25.0",
                "check": 1,
                "dry_time": 0,
                "humidity": "1",
                "humidity_raw": "44",
                "tray": tray_items,
            }
        )

    return {
        "print": {
            "command": "push_status",
            "msg": 0,
            "sequence_id": str(sequence_id),
            "gcode_state": gcode_state,
            "gcode_file": gcode_file,
            "gcode_file_prepare_percent": prepare_percent,
            "subtask_name": gcode_file[:-4] if gcode_file.endswith(".3mf") else gcode_file,
            "print_type": "",
            "mc_print_stage": "",
            "mc_percent": 0,
            "mc_remaining_time": 0,
            "stg": [],
            "stg_cur": 0,
            "layer_num": 0,
            "total_layer_num": 0,
            "print_error": 0,
            "hms": [],
            "home_flag": 256,
            "sdcard": True,
            "storage": {"free": 1000000000, "total": 32000000000},
            "upgrade_state": {
                "sequence_id": 0,
                "progress": "",
                "status": "",
                "consistency_request": False,
                "dis_state": 0,
                "err_code": 0,
                "force_upgrade": False,
                "message": "",
                "module": "",
                "new_version_state": 2,
                "new_ver_list": [],
            },
            "online": {"ahb": False, "rfid": False, "version": 7},
            "nozzle_diameter": "0.4",
            "nozzle_type": "stainless_steel",
            "nozzle_temper": 25.0,
            "bed_temper": 25.0,
            "chamber_temper": 25.0,
            "ams_status": 0,
            "ams_rfid_status": 0,
            "ams": {
                "ams_exist_bits": _bitmask_hex(units),
                # Bitmasks/flags a real AMS reports — only FILLED slots are marked
                # (real prints e.g. '9' for slots 0,3, not 'ffff' for all). These
                # were MISSING, and that's the Device-tab "phantom ABS" cause:
                # tray_read_done_bits is the AMS saying "I have READ + identified
                # the filament in these slots". A recognized Bambu id (GFA0x)
                # resolves from its own id regardless, but a GENERIC spool (GFL99)
                # has no global identity — OrcaSlicer only trusts the slot's
                # reported type once the slot is read-done; otherwise it falls back
                # to the first filament in the model list (alphabetically ABS).
                # Captured from a live A1-mini: tray_*_bits = filled mask,
                # tray_reading_bits '0', insert_flag/power_on_flag True.
                "tray_exist_bits": _bits_from_indices(filled_indices),
                "tray_is_bbl_bits": _bits_from_indices(filled_indices),
                "tray_read_done_bits": _bits_from_indices(filled_indices),
                "tray_reading_bits": "0",
                "tray_now": "255",
                "tray_tar": "255",
                "tray_pre": "255",
                "insert_flag": True,
                "power_on_flag": True,
                # Bambu's Device tab only re-reads the AMS when this version
                # INCREMENTS. It must move whenever the pool changes (and bump on
                # restart) or OrcaSlicer latches the first AMS it ever saw and
                # ignores both later reports and the manual refresh.
                "version": ams_version,
                "ams": ams_units,
            },
            "vt_tray": {
                "id": "254",
                "tray_type": "",
                "tray_color": "00000000",
                "cols": ["00000000"],
                "remain": 0,
            },
        }
    }


# Printer (non-AMS) modules, captured FIELD-FOR-FIELD from a live Bambu Lab
# A1 mini's get_version on 2026-06-15. We mirror reality instead of hand-writing
# approximations: the earlier guessed list (wrong hw_vers MC07/TH07, an invented
# `rv1126` module, the mainboard serial on every module) left the Device tab
# unable to resolve GENERIC filaments (they rendered ABS). Per tuple:
#   name, hw_ver, sw_ver, loader_ver, is_mainboard (product_name=model + visible),
#   own_serial (real printer gives mc/th their OWN sn; ota/esp32 share mainboard's)
# Only the A1 mini is captured so far; other models reuse this base until their
# own get_version is captured. sw_new_ver:"" is present on printer modules (real)
# and ABSENT on the AMS module (also real — do not add it there).
_A1_MINI_BASE_MODULES: list[tuple[str, str, str, str, bool, bool]] = [
    ("ota", "OTA", "01.07.00.00", "00.00.00.00", True, False),
    ("esp32", "AP05", "01.16.39.07", "00.00.00.00", False, False),
    ("mc", "MC02", "00.00.35.57", "00.00.00.32", False, True),
    ("th", "TH03", "00.00.07.72", "00.00.00.26", False, True),
]


def build_get_version(
    model: str,
    serial: str,
    units: int = 4,
    sequence_id: int | str = "0",
    ams_type: str = "n3f",
) -> dict[str, Any]:
    model_display = MODEL_PRODUCT_NAMES.get(model, "X1 Carbon")

    modules: list[dict[str, Any]] = []
    for name, hw_ver, sw_ver, loader_ver, is_mainboard, own_serial in _A1_MINI_BASE_MODULES:
        modules.append(
            {
                "name": name,
                # Real printers put "Bambu Lab <model>" on the mainboard (ota) and
                # leave the other modules' product_name empty.
                "product_name": f"Bambu Lab {model_display}" if is_mainboard else "",
                "sw_ver": sw_ver,
                "sw_new_ver": "",
                "hw_ver": hw_ver,
                "sn": _module_serial(serial, name) if own_serial else serial,
                "loader_ver": loader_ver,
                "visible": is_mainboard,
                "flag": 0,
            }
        )

    # One AMS module per unit. Its `sn` is the unit's `ams_id` (ams_unit_serial),
    # so get_version and push_status agree on the AMS hardware identity — the
    # linkage a real printer has and the Device tab needs to resolve a GENERIC
    # spool. hw_ver/sw_ver/product_name match a real AMS 2 Pro (n3f). NOTE: no
    # sw_new_ver key here — real AMS modules omit it.
    ams_product = {"n3f": "AMS 2 Pro", "n3s": "AMS HT", "ams": "AMS"}.get(ams_type, "AMS")
    ams_hw = {"n3f": "N3F05", "n3s": "AMS_S000", "ams": "AMS08"}.get(ams_type, "AMS08")
    ams_sw = {"n3f": "03.00.21.29"}.get(ams_type, "00.00.06.49")
    for idx in range(max(units, 0)):
        modules.append(
            {
                "name": f"{ams_type}/{idx}",
                "product_name": f"{ams_product} ({idx + 1})",
                "sw_ver": ams_sw,
                "hw_ver": ams_hw,
                "loader_ver": "00.00.00.00",
                "sn": ams_unit_serial(serial, idx),
                "visible": True,
                "flag": 0,
            }
        )
    return {"info": {"command": "get_version", "sequence_id": str(sequence_id), "module": modules}}


def build_print_ack(sequence_id: int | str, gcode_file: str = "", plate: int = 1) -> dict[str, Any]:
    subtask = gcode_file[:-4] if gcode_file.endswith(".3mf") else gcode_file
    return {
        "print": {
            "command": "project_file",
            "sequence_id": str(sequence_id),
            "param": f"Metadata/plate_{plate}.gcode",
            "subtask_name": subtask,
            "gcode_state": "PREPARE",
            "gcode_file": gcode_file,
            "gcode_file_prepare_percent": "0",
            "result": "SUCCESS",
            "msg": 0,
        }
    }


def generate_colors(count: int) -> list[str]:
    if count <= 0:
        return []
    colors: list[str] = []
    hue = 0.0
    step = 0.618033988749895
    for idx in range(count):
        hue = (hue + step) % 1.0
        saturation = 0.70 + 0.20 * (idx % 3) / 2
        value = 0.78 + 0.17 * ((idx + 1) % 4) / 3
        r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
        colors.append(f"{round(r * 255):02X}{round(g * 255):02X}{round(b * 255):02X}FF")
    return colors


MODEL_PRODUCT_NAMES = {
    "N1": "A1 mini",
    "N2S": "A1",
    "3DPrinter-X1-Carbon": "X1 Carbon",
    "BL-P001": "X1 Carbon",
    "BL-P002": "X1",
    "C11": "P1P",
    "C12": "P1S",
    "C13": "X1E",
}


# A real Bambu AMS tray carries these calibration/state fields ALONGSIDE the
# filament identity (tray_type / tray_info_idx / tray_color). OrcaSlicer's Device
# tab uses them to RESOLVE a tray to a known filament. Without them it can still
# resolve RFID-tagged Bambu filaments (they carry tray_info_idx + tray_sub_brands
# the slicer matches against its DB), but for a GENERIC spool — tray_info_idx
# "GFL99", empty sub_brands — it can't, and falls back to the FIRST filament in
# the model's list (alphabetically ABS). That was the "phantom 2 ABS": our two
# Generic-PLA spools rendering as ABS. Captured field-for-field from a real
# A1-mini's Generic-PLA (GFL99) tray on 2026-06-15 (codex-night/gfl99_diff.py)
# so the emulated trays carry the exact shape the Device tab expects.
_REAL_TRAY_DEFAULTS: dict[str, Any] = {
    "bed_temp": "0",
    "bed_temp_type": "0",
    "cali_idx": -1,
    "ctype": 0,
    "k": 0.02,
    "n": 1,
    "state": 3,
    "total_len": 330000,
    "tray_diameter": "0.00",
    "tray_id_name": "",
    "tray_temp": "0",
    "tray_time": "0",
    "tray_weight": "0",
    "xcam_info": "000000000000000000000000",
}


def _with_real_tray_shape(tray: dict[str, Any]) -> dict[str, Any]:
    """Add the calibration/state fields a real Bambu tray always carries so
    OrcaSlicer's Device tab resolves the filament TYPE instead of defaulting an
    unrecognized (Generic / GFL99) spool to the first list entry, ABS. Existing
    keys win — only absent fields are filled."""
    for key, value in _REAL_TRAY_DEFAULTS.items():
        tray.setdefault(key, value)
    return tray


def _empty_tray(slot: int) -> dict[str, Any]:
    # A bare {id} is the correct empty slot — OrcaSlicer renders these as blank
    # just fine (operator-confirmed; the fleet shows many blanks correctly). The
    # "phantom ABS" is NOT from empty slots; do not embellish this.
    return {"id": str(slot)}


def _synthetic_tray(global_idx: int, slot: int, color: str) -> dict[str, Any]:
    material, info_idx, brand, min_temp, max_temp = MATERIALS[global_idx % len(MATERIALS)]
    return _with_real_tray_shape(
        {
            "id": str(slot),
            "tray_type": material,
            "tray_info_idx": info_idx,
            "tray_sub_brands": brand,
            "tray_color": color,
            "cols": [color],
            "nozzle_temp_min": min_temp,
            "nozzle_temp_max": max_temp,
            "remain": 100,
            "tag_uid": f"vp-{global_idx}",
            "tray_uuid": "00000000000000000000000000000000",
        }
    )


def _coerce_pool_tray(tray: dict[str, Any], slot: int) -> dict[str, Any]:
    item = dict(tray)
    item["id"] = str(slot)
    if "tray_type" not in item and isinstance(item.get("material"), str):
        item["tray_type"] = item["material"]
    if "tray_color" not in item and isinstance(item.get("color"), str):
        item["tray_color"] = item["color"]
    color = _normalize_color(str(item.get("tray_color", "FFFFFFFF")))
    item["tray_color"] = color
    cols = item.get("cols")
    item["cols"] = cols if isinstance(cols, list) and cols else [color]
    item.setdefault("tray_info_idx", "")
    item.setdefault("tray_sub_brands", "")
    item.setdefault("nozzle_temp_min", "190")
    item.setdefault("nozzle_temp_max", "230")
    item.setdefault("remain", -1)
    item.setdefault("tag_uid", "0000000000000000")
    item.setdefault("tray_uuid", "00000000000000000000000000000000")
    return _with_real_tray_shape(item)


def _normalize_color(value: str) -> str:
    cleaned = value.strip().lstrip("#").upper()
    if len(cleaned) == 6 and all(ch in "0123456789ABCDEF" for ch in cleaned):
        return cleaned + "FF"
    if len(cleaned) == 8 and all(ch in "0123456789ABCDEF" for ch in cleaned):
        return cleaned
    return "FFFFFFFF"


def _bitmask_hex(bit_count: int) -> str:
    if bit_count <= 0:
        return "0"
    return format((1 << bit_count) - 1, "x")


def _bits_from_indices(indices: list[int]) -> str:
    """Hex bitmask with exactly the given slot indices set (bit i = slot i). A real
    AMS reports tray_exist/read_done/is_bbl as the FILLED-slot mask, e.g. '9' for
    slots 0 and 3 — not every slot. Empty → '0'."""
    mask = 0
    for i in indices:
        if i >= 0:
            mask |= 1 << i
    return format(mask, "x")
