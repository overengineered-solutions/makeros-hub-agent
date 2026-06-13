from __future__ import annotations

import colorsys
from typing import Any


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
) -> dict[str, Any]:
    if units <= 0:
        raise ValueError("units must be positive")
    if trays <= 0:
        raise ValueError("trays must be positive")

    total_trays = units * trays
    colors = generate_colors(total_trays) if filaments is None else []
    ams_units = []
    for unit_idx in range(units):
        tray_items = []
        for tray_idx in range(trays):
            global_idx = unit_idx * trays + tray_idx
            if filaments is None:
                tray = _synthetic_tray(global_idx, tray_idx, colors[global_idx])
            elif global_idx < len(filaments):
                tray = _coerce_pool_tray(filaments[global_idx], tray_idx)
            else:
                tray = _empty_tray(tray_idx)
            tray_items.append(tray)
        ams_units.append(
            {
                "id": str(unit_idx),
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
                "tray_exist_bits": _bitmask_hex(total_trays),
                "tray_now": "255",
                "tray_tar": "255",
                "tray_pre": "255",
                "version": 4,
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


def build_get_version(
    model: str,
    serial: str,
    units: int = 4,
    sequence_id: int | str = "0",
    ams_type: str = "n3f",
) -> dict[str, Any]:
    product = MODEL_PRODUCT_NAMES.get(model, "X1 Carbon")

    def base(name: str, hw_ver: str, sw_ver: str = "01.08.00.00") -> dict[str, Any]:
        return {
            "name": name,
            "product_name": product,
            "sw_ver": sw_ver,
            "sw_new_ver": "",
            "hw_ver": hw_ver,
            "sn": serial,
            "flag": 0,
        }

    modules = [
        base("ota", "OTA"),
        base("esp32", "AP05", "01.07.22.25"),
        base("rv1126", "AP05", "00.00.27.38"),
        base("th", "TH07", "00.00.04.00"),
        base("mc", "MC07", "00.00.10.00"),
    ]
    ams_product = {"n3f": "AMS 2 Pro", "n3s": "AMS HT", "ams": "AMS"}.get(ams_type, "AMS")
    ams_hw = {"n3f": "AMS_F000", "n3s": "AMS_S000", "ams": "AMS08"}.get(ams_type, "AMS08")
    for idx in range(max(units, 0)):
        modules.append(
            {
                "name": f"{ams_type}/{idx}",
                "product_name": ams_product,
                "sw_ver": "00.00.06.49",
                "sw_new_ver": "",
                "hw_ver": ams_hw,
                "sn": f"{serial}-AMS{idx}",
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


def _empty_tray(slot: int) -> dict[str, Any]:
    return {"id": str(slot)}


def _synthetic_tray(global_idx: int, slot: int, color: str) -> dict[str, Any]:
    material, info_idx, brand, min_temp, max_temp = MATERIALS[global_idx % len(MATERIALS)]
    return {
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
    return item


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
