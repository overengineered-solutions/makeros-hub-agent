"""v0.51: the member's own `print` command is captured, shipped to the cloud, handed back in the assignment and
REPLAYED to the physical printer (design B1 + owner decision 6: H2D dual-nozzle). These tests pin the contract:
identity fields are always ours, everything else — ams_mapping2, nozzle fields, member options — is theirs."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from makeros_hub.printers.bambu_send import build_print_start_payload
from makeros_hub.printers.manager import PrinterManager
from makeros_hub.vprinter.capture import CapturedJob, RAW_PRINT_MAX_BYTES, build_vp_submit_body, replayable_print
from makeros_hub.vprinter.outbox import from_record, to_record

RAW = {
    "command": "project_file",
    "sequence_id": "theirs-42",
    "project_id": "9",
    "file": "somewhere-else.3mf",
    "url": "ftp://198.18.10.4/somewhere-else.3mf",
    "param": "Metadata/plate_3.gcode",
    "md5": "deadbeef",
    "subtask_name": "member's own name",
    "bed_type": "cool_plate",
    "flow_cali": True,
    "timelapse": True,
    "use_ams": True,
    "ams_mapping": [4, 5],
    "ams_mapping2": [128, 129],
    "nozzles_info": [{"nozzle_id": 0, "diameter": 0.4}, {"nozzle_id": 1, "diameter": 0.6}],
    "ams_mapping_info": "{\"left\":[0]}",
}


class TestReplayBuilder(unittest.TestCase):
    def test_replay_keeps_member_fields_and_forces_identity(self):
        p = build_print_start_payload("ours.3mf", plate=2, sequence_id="ours-1", raw_print=RAW)["print"]
        # forced: identity + transport are the hub's, never the member's
        self.assertEqual(p["command"], "project_file")
        self.assertEqual(p["file"], "ours.3mf")
        self.assertEqual(p["url"], "ftp:///ours.3mf")
        self.assertEqual(p["param"], "Metadata/plate_2.gcode")
        self.assertEqual(p["md5"], "")
        self.assertEqual(p["sequence_id"], "ours-1")
        self.assertEqual(p["project_id"], "0")
        # kept: the member's own options + the dual-nozzle fields we never enumerate
        self.assertEqual(p["subtask_name"], "member's own name")
        self.assertEqual(p["bed_type"], "cool_plate")
        self.assertIs(p["flow_cali"], True)
        self.assertIs(p["timelapse"], True)
        self.assertIs(p["use_ams"], True)
        self.assertEqual(p["ams_mapping"], [4, 5])
        self.assertEqual(p["ams_mapping2"], [128, 129])
        self.assertEqual(p["nozzles_info"], RAW["nozzles_info"])
        self.assertEqual(p["ams_mapping_info"], RAW["ams_mapping_info"])

    def test_replay_coerces_mappings_and_fills_missing_options(self):
        raw = {"command": "project_file", "ams_mapping": "3,1", "ams_mapping2": "x", "bed_type": "", "use_ams": "yes"}
        p = build_print_start_payload("a.3mf", sequence_id="s", use_ams=False, raw_print=raw)["print"]
        self.assertEqual(p["ams_mapping"], [3, 1])          # string form is what BambuLAN sometimes sends
        self.assertNotIn("ams_mapping2", p)                 # malformed → DROPPED (presence is a dual-nozzle signal)
        self.assertEqual(p["bed_type"], "textured_plate")   # empty member value → our default
        self.assertIs(p["use_ams"], False)                  # non-bool member value → the argument wins
        self.assertEqual(p["subtask_name"], "a")            # no member name → derived from the file

    def test_replay_drops_identity_aliases(self):
        raw = {**RAW, "gcode_file": "other.gcode", "plate": 7}
        p = build_print_start_payload("ours.3mf", plate=2, sequence_id="s", raw_print=raw)["print"]
        self.assertNotIn("gcode_file", p)
        self.assertNotIn("plate", p)
        self.assertEqual(p["param"], "Metadata/plate_2.gcode")   # the forced selector is the only plate the printer sees

    def test_replayable_print_survives_pathological_nesting(self):
        from makeros_hub.vprinter.capture import replayable_print as rp
        deep: dict = {}
        cur = deep
        for _ in range(5000):
            cur["n"] = {}
            cur = cur["n"]
        self.assertIsNone(rp(deep))

    def test_no_raw_print_is_the_pre_v051_command(self):
        legacy = build_print_start_payload("a.3mf", sequence_id="s", plate=1, use_ams=True, ams_mapping=[0])["print"]
        also = build_print_start_payload("a.3mf", sequence_id="s", plate=1, use_ams=True, ams_mapping=[0], raw_print={})["print"]
        self.assertEqual(legacy, also)
        self.assertNotIn("ams_mapping2", legacy)


class TestCaptureAndOutbox(unittest.TestCase):
    def _job(self, raw):
        return CapturedJob(
            member_id="m1", filename="a.3mf", file_path=Path("/tmp/a.3mf"), sha256="0" * 64, size=3,
            ams_mapping={"ams_mapping": [4, 5], "ams_mapping2": [128]}, use_ams=True, required_filaments=[],
            submitted_at=datetime(2026, 8, 29, tzinfo=timezone.utc), submission_uid="abc123", plate=3,
            vp_serial="00M09VP1", vp_model="H2D", raw_print=raw,
        )

    def test_replayable_print_bounds(self):
        self.assertIsNone(replayable_print(None))
        self.assertIsNone(replayable_print({}))
        self.assertIsNone(replayable_print({"x": "y" * (RAW_PRINT_MAX_BYTES + 1)}))
        self.assertEqual(replayable_print(RAW), json.loads(json.dumps(RAW)))

    def test_outbox_round_trip_and_body(self):
        job = self._job(RAW)
        back = from_record(json.loads(json.dumps(to_record(job))))
        self.assertEqual(back.raw_print, RAW)
        body = build_vp_submit_body(job, model="H2D")
        self.assertEqual(body["rawPrint"], RAW)
        self.assertEqual(body["amsMapping"], [4, 5])            # the cloud contract stays a flat list
        self.assertEqual(body["amsMappingRaw"], {"ams_mapping": [4, 5], "ams_mapping2": [128]})
        self.assertNotIn("rawPrint", build_vp_submit_body(self._job(None), model="H2D"))
        self.assertIsNone(from_record({"submission_uid": "x", "rawPrint": "not-a-dict"}).raw_print)


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def start_print(self, local_path, file_name, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}


class TestDispatchCarriesRawPrint(unittest.TestCase):
    def _dispatch(self, assignment):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "abcdef12" / "part.3mf"
            f.parent.mkdir()
            f.write_bytes(b"3mf")
            manager = PrinterManager()
            fake = FakeAdapter()
            manager._adapters["p1"] = fake
            manager.dispatch_assignments([assignment], d)
        return fake.calls

    def test_raw_print_reaches_the_adapter_only_when_it_is_a_dict(self):
        base = {"queueJobId": "q1", "printerId": "p1", "submissionUid": "abcdef12", "fileName": "part.3mf", "plate": 2}
        self.assertEqual(self._dispatch({**base, "rawPrint": RAW})[0]["raw_print"], RAW)
        self.assertIsNone(self._dispatch(base)[0]["raw_print"])
        self.assertIsNone(self._dispatch({**base, "rawPrint": "garbage"})[0]["raw_print"])
        self.assertIsNone(self._dispatch({**base, "rawPrint": {}})[0]["raw_print"])


if __name__ == "__main__":
    unittest.main()


class TestUsedGrams(unittest.TestCase):
    def test_slice_info_used_g_rides_as_usedG_and_estGrams(self):
        from makeros_hub.vprinter.capture import parse_slice_info_config, _vp_submit_filament
        items = parse_slice_info_config(
            '<config><filament id="0" type="PLA" color="#ffffff" used_m="1.2" used_g="3.67"/>'
            '<filament id="1" type="PETG" color="#000000" used_g="0.5"/><filament id="2" type="TPU" used_g="nope"/></config>'
        )
        self.assertEqual([i.get("usedG") for i in items], [3.67, 0.5, None])
        self.assertEqual(_vp_submit_filament(items[0]), {"slot": 0, "type": "PLA", "color": "FFFFFFFF", "usedG": 3.67})
        job = CapturedJob(member_id="m", filename="a.3mf", file_path=Path("/tmp/a.3mf"), sha256="0" * 64, size=1, ams_mapping=[0],
                          use_ams=True, required_filaments=items, submitted_at=datetime(2026, 8, 30, tzinfo=timezone.utc), submission_uid="u1")
        body = build_vp_submit_body(job, model="A1 mini")
        self.assertEqual(body["estGrams"], 5)                      # ceil(3.67 + 0.5) — never undercharge
        job2 = CapturedJob(member_id="m", filename="a.3mf", file_path=Path("/tmp/a.3mf"), sha256="0" * 64, size=1, ams_mapping=[0],
                           use_ams=True, required_filaments=[{"slot": 0, "material": "PLA"}], submitted_at=datetime(2026, 8, 30, tzinfo=timezone.utc), submission_uid="u2")
        self.assertNotIn("estGrams", build_vp_submit_body(job2, model="A1 mini"))
