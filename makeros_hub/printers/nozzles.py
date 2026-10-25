"""The file's own nozzle requirements, checked again at the physical printer before a start."""
import math


def nozzle_refusal(required: list | None, hardware: dict, *, require_report: bool = False) -> str | None:
    if not required or any(not isinstance(f, dict) or isinstance(f.get("nozzleDiameterMm"), bool) or
                           not isinstance(f.get("nozzleDiameterMm"), (int, float)) or
                           not math.isfinite(f["nozzleDiameterMm"]) or not 0 < f["nozzleDiameterMm"] <= 2 for f in required):
        return "nozzle_unknown: slice the file with the shop's printer preset and send it again"
    nozzles = hardware.get("nozzles") or []
    dual = hardware.get("extruderCount") == 2
    if not nozzles:
        return "nozzle_unknown: waiting for the printer's nozzle report" if require_report or dual else None
    if dual and any(len([n for n in nozzles if n.get("extruderId") == eid]) != 1 for eid in (0, 1)):
        return "nozzle_unknown: waiting for both printer nozzles to be reported"
    for f in required:
        eid = f.get("extruderId")
        if dual and (type(eid) is not int or eid not in (0, 1)):
            return "nozzle_unknown: the file does not identify each filament's nozzle"
        actual = [n for n in nozzles if n.get("extruderId") == eid] if dual else nozzles
        if len(actual) != 1 or actual[0].get("diameterMm") != f["nozzleDiameterMm"]:
            return "nozzle_mismatch: the sliced nozzle size differs from the printer; slice with the shop's printer preset"
    return None
