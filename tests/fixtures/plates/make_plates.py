"""Synthetic sliced 3MFs in the three shapes members' failed prints had on 2026-10-22, plus one with several plates. Nothing in
them comes from a member's file: made-up names, a few lines of G-code, an eight-byte picture. Real zips (CRCs set, text deflated,
pictures stored as OrcaSlicer stores them, fixed timestamps), so the bytes are the same every run; the site repo keeps byte-equal
copies (test/fixtures/plates) and both suites pin their sha256, so one file is read the same way on both sides.

    python3 tests/fixtures/plates/make_plates.py   # rewrites the .3mf files beside it
"""

import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAMP = (2026, 10, 22, 12, 0, 0)

# name: (sliced plates, every plate of the project (pictures), printer_model_id)
FIXTURES = {
    "single-plate-2.3mf": ([2], [1, 2], "BL-P001"),       # OrcaSlicer 2.4.2 Export plate sliced file of plate 2: only plate_2.gcode
    "send-plate-3.3mf": ([3], [1, 2, 3, 4, 5], "N7"),     # an OrcaSlicer Send of plate 3 of five ("…_plate_3")
    "part_P3.3mf": ([3], [1, 2, 3], "N1"),                # a plate-3 export the member named "_P3"
    "three-plates.3mf": ([1, 2, 3], [1, 2, 3], "N1"),     # every plate sliced: the member must choose
}


def slice_info(plates, model):
    body = "".join(
        f'<plate><metadata key="index" value="{n}"/><metadata key="printer_model_id" value="{model}"/>'
        f'<filament id="1" tray_info_idx="GFL99" type="PLA" color="#FFFFFF" used_m="1.10" used_g="3.40"/></plate>'
        for n in plates
    )
    return ('<?xml version="1.0" encoding="UTF-8"?><config><header><header_item key="OrcaSlicer-Version" value="2.4.2"/></header>'
            f"{body}</config>")


def write(path, sliced, pictures, model):
    with zipfile.ZipFile(path, "w") as zf:
        def put(name, data, method):
            info = zipfile.ZipInfo(name, date_time=STAMP)
            info.compress_type = method
            zf.writestr(info, data)
        put("[Content_Types].xml", '<?xml version="1.0"?><Types/>', zipfile.ZIP_DEFLATED)
        for n in pictures:
            put(f"Metadata/plate_{n}.png", b"\x89PNG\r\n\x1a\n" + bytes([n]), zipfile.ZIP_STORED)
        put("3D/3dmodel.model", '<model unit="millimeter"><resources/><build/></model>', zipfile.ZIP_DEFLATED)
        for n in sliced:
            put(f"Metadata/plate_{n}.gcode", f"; synthetic plate {n}\nG28\nG1 Z5\n", zipfile.ZIP_DEFLATED)
        put("Metadata/slice_info.config", slice_info(sliced, model), zipfile.ZIP_DEFLATED)


if __name__ == "__main__":
    for name, (sliced, pictures, model) in FIXTURES.items():
        write(HERE / name, sliced, pictures, model)
        print(name)
