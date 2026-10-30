"""Synthetic slices using the plate mapping layout verified in local H2D exports."""
import io
import json
import zipfile

import pytest

from makeros_hub.printers.ingest_facts import POOLS, read_file_facts
from test_moonraker import gcode, threemf


MISSING_MAP = "This file does not say which nozzle each filament uses. Reslice it for the PS H2D preset."
NOZZLE_COUNT = "This file's G-code must name exactly two nozzle diameters. Reslice it for the PS H2D preset."


def h2d_threemf(used=(1, 4), *, nozzles="0.4,0.4", mapping="complete", sizes=("0.4", "0.4"),
               model="PS H2D", model_id="O1D", stats=True):
    # Sparse filament IDs; logical 0 is physical 1 (left), logical 1 is physical 0 (right).
    xml = (f'<config><plate><metadata key="index" value="1"/>'
           f'<metadata key="printer_model_id" value="{model_id}"/>')
    if mapping != "no_plate_map":
        value = "2 2 2 2" if mapping == "conflict" else "1 2 2 2"
        xml += f'<metadata key="filament_maps" value="{value}"/>'
    for slot in used:
        group = 0 if slot == 1 else 1
        attrs = f' group_id="{group}"' if mapping != "no_groups" else ""
        if sizes is not None:
            attrs += f' nozzle_diameter="{sizes[group]}"'
        xml += f'<filament id="{slot}" type="PLA" color="#FFFFFF" used_g="1.25"{attrs}/>'
    data = gcode(model, mm=", ".join("100" if i in used else "0" for i in range(1, 5)),
                 g=", ".join("1.25" if i in used else "0" for i in range(1, 5)),
                 types="PLA;PLA;PLA;PLA", colours="#FFFFFF;#FFFFFF;#FFFFFF;#FFFFFF")
    data = data.replace(b"; nozzle_diameter = 0.4\n", f"; nozzle_diameter = {nozzles}\n".encode())
    if not stats:
        data = b"\n".join(line for line in data.split(b"\n") if not line.startswith(b"; filament used ["))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Metadata/plate_1.gcode", data)
        archive.writestr("Metadata/slice_info.config", xml + "</plate></config>")
        if mapping != "no_physical_map":
            # The project default can be stale: only the selected plate decides.
            archive.writestr("Metadata/project_settings.config", json.dumps({
                "physical_extruder_map": ["1", "0"], "filament_map": ["1", "1", "1", "1"]}))
    return buffer.getvalue()


def facts(tmp_path, **kwargs):
    path = tmp_path / "h2d.3mf"
    path.write_bytes(h2d_threemf(**kwargs))
    return read_file_facts(path, "h2d")


@pytest.mark.parametrize("used", [(1, 4), (1,), (4,)], ids=["both", "left", "right"])
def test_each_used_filament_names_its_physical_extruder(tmp_path, used):
    got = facts(tmp_path, used=used)
    assert got["requiredFilaments"] == [
        {"slot": slot, "type": "PLA", "color": "FFFFFFFF", "usedG": 1.25,
         "extruderId": 1 if slot == 1 else 0, "nozzleDiameterMm": 0.4} for slot in used]
    assert got["estGrams"] == (3 if len(used) == 2 else 2)
    assert (got["modelPool"], got["printerModelId"], got["plate"]) == ("h2d", "O1D", 1)


@pytest.mark.parametrize("sizes", [("0.6", "0.4"), None], ids=["cross_checked", "from_gcode"])
def test_nozzle_sizes_follow_logical_groups_not_physical_ids_or_filament_slots(tmp_path, sizes):
    got = facts(tmp_path, nozzles="0.6,0.4", sizes=sizes)
    assert [(f["extruderId"], f["nozzleDiameterMm"]) for f in got["requiredFilaments"]] == [(1, .6), (0, .4)]


def test_plate_supplies_used_slots_when_gcode_omits_usage_statistics(tmp_path):
    assert facts(tmp_path, stats=False) == facts(tmp_path)


@pytest.mark.parametrize("mapping", ["no_plate_map", "no_groups", "no_physical_map", "conflict"])
def test_missing_or_conflicting_plate_mapping_never_uses_project_default(tmp_path, mapping):
    with pytest.raises(ValueError) as error:
        facts(tmp_path, mapping=mapping)
    assert str(error.value) == MISSING_MAP


@pytest.mark.parametrize("nozzles", ["", "0.4", "0.4,0.4,0.4"])
def test_h2d_requires_two_header_nozzles_even_for_a_one_nozzle_job(tmp_path, nozzles):
    with pytest.raises(ValueError) as error:
        facts(tmp_path, used=(1,), nozzles=nozzles)
    assert str(error.value) == NOZZLE_COUNT


@pytest.mark.parametrize("nozzles", ["0.4,nan", "0.4,0", "0.4,3"])
def test_neither_header_nozzle_can_have_an_unknown_or_invalid_size(tmp_path, nozzles):
    with pytest.raises(ValueError, match="valid nozzle diameters"):
        facts(tmp_path, used=(1,), nozzles=nozzles)


def test_plate_and_header_nozzle_disagreement_is_refused(tmp_path):
    with pytest.raises(ValueError, match="filament facts disagree"):
        facts(tmp_path, nozzles="0.6,0.4")


@pytest.mark.parametrize("pool", ["sv08-max", "giga", "p2s", "a1-mini", "x1c"])
def test_single_extruder_facts_stay_identical(tmp_path, pool):
    model, aliases, model_id = POOLS[pool]
    profile = next(a for a in aliases if a.startswith("PS "))
    path = tmp_path / ("part.3mf" if model_id else "part.gcode")
    path.write_bytes(threemf(model=profile, model_id=model_id) if model_id else gcode(profile))
    assert read_file_facts(path, pool) == {
        "version": 1, "modelPool": pool, "printerModel": model, "sourcePrinterModel": profile,
        "printerModelId": model_id, "plate": 1, "bedType": "Textured PEI Plate", "estGrams": 5,
        "requiredFilaments": [{"slot": 1, "type": "PLA", "color": "FFFFFFFF", "usedG": 4.53,
                               "nozzleDiameterMm": .4}]}


@pytest.mark.parametrize("used,message", [
    ((4,), "Selected plate must name each filament once, on a single extruder"),
    ((1,), "This model URL does not support a second extruder"),
])
def test_single_extruder_refusals_keep_their_words(tmp_path, used, message):
    path = tmp_path / "p2s.3mf"
    path.write_bytes(h2d_threemf(used, model="PS P2S", model_id="N7", nozzles="0.4"))
    with pytest.raises(ValueError) as error:
        read_file_facts(path, "p2s")
    assert str(error.value) == message
