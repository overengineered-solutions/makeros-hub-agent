"""Reported presence is independent from filament settings and RFID identity."""
import pytest
from makeros_hub.printers.bambu_parse import build_ams, build_vt_tray
from makeros_hub.printers.tray_translate import live_trays, cleared_tray, translate_print_trays, TrayTranslationError
from makeros_hub.vprinter.live_pool import tray_key


def report(state, **extra):
    return {"id": "2", "state": state, "tray_type": "PETG", "tray_color": "FFFFFF00", "tray_info_idx": "GFG99", **extra}


def normalized(raw):
    units = build_ams({"ams": {"ams": [{"id": "0", "tray": [raw]}]}})
    return units, units[0]["trays"][0]


@pytest.mark.parametrize("state,present", [(0, False), (3, True), (8, False), (9, True), (10, False), (11, True), ("11", True)])
def test_preserves_sensor_presence_for_both_holders(state, present):
    raw = report(state)
    _, tray = normalized(raw)
    assert tray["spoolPresent"] is present
    assert build_vt_tray({"vt_tray": raw})["spoolPresent"] is present


@pytest.mark.parametrize("state", [None, True, -1, 1.5, "bad", 2**32, 1, 2, 5, 17, 21])
def test_missing_invalid_or_transient_presence_is_unknown(state):
    _, tray = normalized(report(state))
    assert "spoolPresent" not in tray


def test_generic_clear_detected_spool_reaches_pool_and_physical_mapping():
    units, tray = normalized(report(11))
    assert tray_key(tray) is not None
    assert len(live_trays(units)) == 1
    cmd = translate_print_trays({"use_ams": True, "ams_mapping": [0]}, [{"slot": 1, "type": "PETG", "color": "FFFFFF00", "idx": "GFG99"}], units)
    assert cmd["ams_mapping"] == [tray["slot"]]


@pytest.mark.parametrize("color", ["FFFFFF00", "000000FF"])
def test_explicit_absence_overrides_saved_identity_even_with_rfid(color):
    units, tray = normalized(report(8, tray_color=color, tag_uid="AA6A2BF200000100"))
    assert tray["material"] == "PETG"  # display retains what the printer reported
    assert tray_key(tray) is None
    assert live_trays(units) == []
    with pytest.raises(TrayTranslationError):
        translate_print_trays({"use_ams": True, "ams_mapping": [0]}, [{"slot": 1, "type": "PETG", "color": color}], units)


def test_only_a_real_boolean_is_presence_evidence():
    for present in [None, "true", 1, {}, []]:
        assert cleared_tray({"material": "PETG", "colorHex": "FFFFFF00", "spoolPresent": present})
    assert not cleared_tray({"material": "PETG", "colorHex": "FFFFFF00", "spoolPresent": True})
