from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import tempfile
import unittest
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from makeros_hub.config import VirtualPrinterMember, parse_virtual_printer_config
from makeros_hub.vprinter.ftp_server import FtpConfig, FtpSession
from makeros_hub.vprinter.auth import AuthRateLimiter, MemberAuthSet
from makeros_hub.vprinter.bind_server import END_MAGIC, START_MAGIC, decode_frame, encode_frame
from makeros_hub.vprinter.capture import (
    CaptureCoordinator,
    CapturedJob,
    ProjectFileIntent,
    UploadRecord,
    assemble_captured_job,
    build_vp_submit_body,
    parse_project_file_command,
    parse_required_filaments,
    parse_slice_info_config,
)
from makeros_hub.vprinter.mqtt_broker import (
    ConnectPacket,
    MAX_MQTT_PACKET_BYTES,
    MqttBroker,
    build_publish,
    decode_remaining_length_from_bytes,
    encode_remaining_length,
    parse_connect,
    parse_publish,
    _read_packet,
)
from makeros_hub.vprinter.report import build_get_version, build_print_ack, build_push_status


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class TestVirtualPrinterConfig(unittest.TestCase):
    def test_parse_enabled_block_normalizes_members_and_pool(self):
        code_hash = _code_hash("12345678")
        cfg = parse_virtual_printer_config(
            {
                "enabled": True,
                "serial": "SER123",
                "model": "N1",
                "name": "VP A1",
                "fw": "01.08.00.00",
                "bind_ip": "100.64.0.10",
                "members": [{"access_code_sha256": code_hash, "member_id": "m1"}],
                "pool": [{"material": "PLA", "color": "#abcdef", "tray_info_idx": "GFA00"}],
            }
        )

        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.serial, "SER123")
        self.assertEqual(cfg.members[0].access_code_sha256, code_hash)
        self.assertEqual(cfg.members[0].member_id, "m1")
        self.assertEqual(cfg.pool[0]["tray_type"], "PLA")
        self.assertEqual(cfg.pool[0]["tray_color"], "ABCDEFFF")

    def test_parse_accepts_camel_case_config_and_member_fields(self):
        code_hash = _code_hash("ABCDEFGH").upper()
        cfg = parse_virtual_printer_config(
            {
                "enabled": True,
                "serial": "SER123",
                "model": "3DPrinter-X1-Carbon",
                "name": "VP A1",
                "fw": "01.08.00.00",
                "bindIp": "100.64.0.10",
                "units": 4,
                "trays": 4,
                "amsType": "n3f",
                "members": [{"accessCodeSha256": f" {code_hash} ", "memberId": " m1 "}],
                "pool": [{"tray_type": "PLA", "tray_info_idx": "GFL99"}],
            }
        )

        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.bind_ip, "100.64.0.10")
        self.assertEqual(cfg.ams_type, "n3f")
        self.assertEqual(cfg.members, (VirtualPrinterMember(_code_hash("ABCDEFGH"), "m1"),))
        self.assertEqual(cfg.pool[0]["tray_info_idx"], "GFL99")

    def test_parse_bad_shape_skips(self):
        self.assertIsNone(parse_virtual_printer_config(None))
        self.assertIsNone(parse_virtual_printer_config({"enabled": False}))
        self.assertIsNone(parse_virtual_printer_config({"enabled": "false"}))
        self.assertIsNone(parse_virtual_printer_config({"enabled": 1}))
        self.assertIsNone(parse_virtual_printer_config({"enabled": True, "bind_ip": "bad"}))

    def test_parse_validates_member_access_code_hashes(self):
        valid_hash = _code_hash("ABCD1234")
        cfg = parse_virtual_printer_config(
            {
                "enabled": True,
                "serial": "SER123",
                "model": "N1",
                "name": "VP A1",
                "fw": "01.08.00.00",
                "bind_ip": "100.64.0.10",
                "members": [
                    {"access_code_sha256": f" {valid_hash.upper()} ", "member_id": " m1 "},
                    {"access_code_sha256": "f" * 63, "member_id": "short"},
                    {"access_code_sha256": "g" * 64, "member_id": "badhex"},
                    {"access_code_sha256": "f" * 65, "member_id": "long"},
                    {"access_code_sha256": valid_hash, "member_id": "duplicate"},
                    {"access_code_sha256": "        ", "member_id": "blank"},
                    {"access_code_sha256": _code_hash("missing"), "member_id": ""},
                    {"access_code_sha256": _code_hash("missing-id")},
                ],
            }
        )

        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.members, (VirtualPrinterMember(valid_hash, "m1"),))
        self.assertIsNone(
            parse_virtual_printer_config(
                {
                    "enabled": True,
                    "serial": "SER123",
                    "model": "N1",
                    "name": "VP A1",
                    "fw": "01.08.00.00",
                    "bind_ip": "100.64.0.10",
                    "members": [{"access_code_sha256": "z" * 64, "member_id": "m1"}],
                }
            )
        )

    def test_units_are_capped_to_four(self):
        cfg = parse_virtual_printer_config(
            {
                "enabled": True,
                "serial": "SER123",
                "model": "N1",
                "name": "VP A1",
                "fw": "01.08.00.00",
                "bind_ip": "100.64.0.10",
                "units": 4,
                "members": [{"access_code_sha256": _code_hash("12345678"), "member_id": "m1"}],
            }
        )

        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.units, 4)
        self.assertIsNone(
            parse_virtual_printer_config(
                {
                    "enabled": True,
                    "serial": "SER123",
                    "model": "N1",
                    "name": "VP A1",
                    "fw": "01.08.00.00",
                    "bind_ip": "100.64.0.10",
                    "units": 5,
                    "members": [{"access_code_sha256": _code_hash("12345678"), "member_id": "m1"}],
                }
            )
        )


class TestReportBuilders(unittest.TestCase):
    def test_push_status_16_slots_two_filled_ams_2_pro(self):
        report = build_push_status(
            units=4,
            trays=4,
            sequence_id=7,
            filaments=[
                {"tray_type": "PLA", "tray_info_idx": "GFA00", "tray_color": "FFFFFFFF"},
                {"material": "PETG", "color": "#11223344", "tray_info_idx": "GFG00"},
            ],
        )
        payload = report["print"]
        ams = payload["ams"]
        trays = [tray for unit in ams["ams"] for tray in unit["tray"]]

        self.assertEqual(payload["command"], "push_status")
        self.assertEqual(payload["sdcard"], True)
        self.assertEqual(payload["home_flag"], 256)
        self.assertEqual(ams["ams_exist_bits"], "f")
        self.assertEqual(ams["tray_exist_bits"], "ffff")
        self.assertEqual(len(ams["ams"]), 4)
        self.assertEqual(sum(len(unit["tray"]) for unit in ams["ams"]), 16)
        self.assertTrue(all(unit["info"] == "2003" for unit in ams["ams"]))
        self.assertEqual(trays[0]["tray_type"], "PLA")
        self.assertEqual(trays[1]["tray_type"], "PETG")
        self.assertEqual(trays[2], {"id": "2"})

    def test_get_version_has_n3f_modules_and_ack_shape(self):
        version = build_get_version("N1", "SER123", units=4, sequence_id="abc", ams_type="n3f")
        modules = version["info"]["module"]

        self.assertEqual(version["info"]["sequence_id"], "abc")
        self.assertIn("ota", [module["name"] for module in modules])
        self.assertEqual([m["name"] for m in modules if m["name"].startswith("n3f/")], ["n3f/0", "n3f/1", "n3f/2", "n3f/3"])
        self.assertTrue(all(m["product_name"] == "AMS 2 Pro" for m in modules if m["name"].startswith("n3f/")))

        ack = build_print_ack("9", "part.3mf")
        self.assertEqual(ack["print"]["command"], "project_file")
        self.assertEqual(ack["print"]["result"], "SUCCESS")
        self.assertEqual(ack["print"]["gcode_state"], "PREPARE")


class TestBindFrame(unittest.TestCase):
    def test_encode_decode_frame(self):
        payload = {"login": {"command": "detect", "sequence_id": "20000"}}
        frame = encode_frame(payload)
        start_magic, total_len = struct.unpack("<HH", frame[:4])
        (end_magic,) = struct.unpack("<H", frame[-2:])

        self.assertEqual(start_magic, START_MAGIC)
        self.assertEqual(total_len, len(frame))
        self.assertEqual(end_magic, END_MAGIC)
        self.assertEqual(decode_frame(frame), payload)

    def test_decode_rejects_bad_trailer(self):
        frame = bytearray(encode_frame({"ok": True}))
        frame[-1] = 0
        with self.assertRaises(ValueError):
            decode_frame(bytes(frame))


class TestMqttCodec(unittest.TestCase):
    def test_remaining_length_boundaries(self):
        values = [0, 1, 127, 128, 16_383, 16_384, 2_097_151, 2_097_152, 268_435_455]
        for value in values:
            with self.subTest(value=value):
                encoded = encode_remaining_length(value)
                decoded, consumed = decode_remaining_length_from_bytes(encoded)
                self.assertEqual(decoded, value)
                self.assertEqual(consumed, len(encoded))

    def test_parse_connect_and_publish(self):
        payload = (
            _mqtt_string("MQTT")
            + bytes([4, 0xC2])
            + (60).to_bytes(2, "big")
            + _mqtt_string("orca-client")
            + _mqtt_string("bblp")
            + _mqtt_string("12345678")
        )
        parsed = parse_connect(payload)
        self.assertEqual(parsed.protocol_name, "MQTT")
        self.assertEqual(parsed.protocol_level, 4)
        self.assertEqual(parsed.username, "bblp")
        self.assertEqual(parsed.password, "12345678")

        packet = build_publish("device/serial/report", b'{"ok":true}')
        remaining, consumed = decode_remaining_length_from_bytes(packet[1:])
        publish = parse_publish(packet[0], packet[1 + consumed : 1 + consumed + remaining])
        self.assertEqual(publish.topic, "device/serial/report")
        self.assertEqual(publish.payload, b'{"ok":true}')

    def test_read_packet_rejects_oversized_remaining_length_before_payload(self):
        async def run():
            reader = asyncio.StreamReader()
            reader.feed_data(b"\x10" + encode_remaining_length(MAX_MQTT_PACKET_BYTES + 1))
            reader.feed_eof()
            with self.assertRaises(ValueError):
                await _read_packet(reader)

        asyncio.run(run())


class TestMemberAuth(unittest.TestCase):
    def test_lookup_is_member_attributing_and_checks_all_codes(self):
        auth = MemberAuthSet(
            [
                VirtualPrinterMember(_code_hash("11111111"), "m1"),
                VirtualPrinterMember(_code_hash("22222222"), "m2"),
                VirtualPrinterMember(_code_hash("33333333"), "m3"),
            ]
        )

        with mock.patch("hmac.compare_digest", wraps=__import__("hmac").compare_digest) as compare:
            result = auth.authenticate("22222222", "100.64.0.20")

        self.assertTrue(result.ok)
        self.assertEqual(result.member_id, "m2")
        self.assertEqual(compare.call_count, 3)

    def test_wrong_empty_and_none_codes_do_not_match(self):
        auth = MemberAuthSet([VirtualPrinterMember(_code_hash("12345678"), "m1")])

        self.assertIsNone(auth.lookup_member_id("wrongcode"))
        self.assertIsNone(auth.lookup_member_id(""))
        self.assertIsNone(auth.lookup_member_id(None))

    def test_short_code_compares_every_member_but_never_matches_hash(self):
        short_hash = _code_hash("short")
        auth = MemberAuthSet(
            [
                VirtualPrinterMember(short_hash, "short-member"),
                VirtualPrinterMember(_code_hash("12345678"), "m1"),
            ]
        )

        with mock.patch("hmac.compare_digest", wraps=__import__("hmac").compare_digest) as compare:
            result = auth.authenticate("short", "100.64.0.20")

        self.assertFalse(result.ok)
        self.assertEqual(compare.call_count, 2)

    def test_rate_limit_is_per_ip_and_per_code(self):
        now = [1000.0]
        limiter = AuthRateLimiter(clock=lambda: now[0])
        auth = MemberAuthSet([VirtualPrinterMember(_code_hash("12345678"), "m1")], limiter=limiter)

        for _ in range(5):
            self.assertFalse(auth.authenticate("badcode", "100.64.0.20").rate_limited)

        self.assertTrue(auth.authenticate("otherbad", "100.64.0.20").rate_limited)
        self.assertTrue(auth.authenticate("badcode", "100.64.0.21").rate_limited)
        now[0] += 61.0
        self.assertFalse(auth.authenticate("badcode", "100.64.0.20").rate_limited)

    def test_rate_limiter_prunes_and_caps_random_keys(self):
        now = [1000.0]
        limiter = AuthRateLimiter(window_sec=10.0, max_keys=3, clock=lambda: now[0])

        for idx in range(20):
            limiter.record_failure(f"100.64.0.{idx}", f"bad{idx:05d}")

        self.assertLessEqual(len(limiter._by_ip), 3)
        self.assertLessEqual(len(limiter._by_code), 3)
        now[0] += 11.0
        self.assertFalse(limiter.is_limited("100.64.0.200", "bad99999"))
        self.assertEqual(len(limiter._by_ip), 0)
        self.assertEqual(len(limiter._by_code), 0)

    def test_mqtt_and_ftp_auth_failures_are_recorded_on_bypass_branches(self):
        limiter = AuthRateLimiter(limit=1)
        auth = MemberAuthSet([VirtualPrinterMember(_code_hash("12345678"), "m1")], limiter=limiter)
        broker = MqttBroker(
            serial="SER123",
            auth=auth,
            report_builder=lambda *_args: {},
            version_builder=lambda _seq: {},
            ack_builder=lambda _seq, _file: {},
            on_project_file=None,
            log=lambda _msg: None,
        )

        proto_rc, _ = broker._auth_connect(
            ConnectPacket("MQIsdp", 3, 0, 60, "client", "bblp", "badproto", True),
            "100.64.0.30",
        )
        user_rc, _ = broker._auth_connect(
            ConnectPacket("MQTT", 4, 0, 60, "client", "wrong", "baduser", True),
            "100.64.0.31",
        )

        self.assertEqual(proto_rc, 0x01)
        self.assertEqual(user_rc, 0x04)
        self.assertTrue(limiter.is_limited("100.64.0.30", "badproto"))
        self.assertTrue(limiter.is_limited("100.64.0.31", "baduser"))

        async def run_ftp_wrong_user():
            with tempfile.TemporaryDirectory() as d:
                session = _ftp_session(
                    FtpConfig("100.64.0.10", Path(d), auth),
                    peer=("100.64.0.32", 12345),
                )
                session.user = "wrong"
                await session.cmd_pass("badftp")

        asyncio.run(run_ftp_wrong_user())
        self.assertTrue(limiter.is_limited("100.64.0.32", "badftp"))


class TestSliceInfoParser(unittest.TestCase):
    def test_parses_metadata_arrays(self):
        xml = """
        <config>
          <metadata key="filament_type" value="PLA;PETG"/>
          <metadata key="filament_colour" value="#ff0000;00ff00ff"/>
        </config>
        """

        self.assertEqual(
            parse_slice_info_config(xml),
            [
                {"slot": 0, "material": "PLA", "color": "FF0000FF"},
                {"slot": 1, "material": "PETG", "color": "00FF00FF"},
            ],
        )

    def test_parses_3mf_slice_info_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "job.3mf"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "Metadata/slice_info.config",
                    '<config><filament id="0" type="PLA" color="#abcdef"/></config>',
                )

            self.assertEqual(
                parse_required_filaments(path),
                [{"slot": 0, "material": "PLA", "color": "ABCDEFFF"}],
            )


class TestCaptureAssembly(unittest.TestCase):
    def test_build_vp_submit_body_maps_cloud_contract_fields(self):
        job = CapturedJob(
            member_id="member-1",
            filename="part.3mf",
            file_path=Path("part.3mf"),
            sha256="a" * 64,
            size=123,
            ams_mapping=[0, 1],
            use_ams=True,
            required_filaments=[
                {"slot": 0, "material": "PLA", "color": "FFFFFFFF", "tray_info_idx": "GFL99"}
            ],
            submitted_at=datetime(2026, 6, 13, tzinfo=timezone.utc),
            submission_uid="submission-1",
            plate=1,
        )

        body = build_vp_submit_body(job, model="3DPrinter-X1-Carbon")

        self.assertEqual(
            body,
            {
                "hubSubmissionUid": "submission-1",
                "memberId": "member-1",
                "fileName": "part.3mf",
                "fileSha256": "a" * 64,
                "fileSizeBytes": 123,
                "printerModel": "3DPrinter-X1-Carbon",
                "useAms": True,
                "amsMapping": [0, 1],
                "requiredFilaments": [
                    {"slot": 0, "type": "PLA", "color": "FFFFFFFF", "trayInfoIdx": "GFL99"}
                ],
                "plate": 1,
            },
        )
        self.assertNotIn("accessCode", body)
        self.assertNotIn("access_code", body)

    def test_build_vp_submit_body_omits_absent_plate_and_uid_is_stable(self):
        job = CapturedJob(
            member_id="member-1",
            filename="part.3mf",
            file_path=Path("part.3mf"),
            sha256="b" * 64,
            size=456,
            ams_mapping=[],
            use_ams=False,
            required_filaments=[],
            submitted_at=datetime(2026, 6, 13, tzinfo=timezone.utc),
        )

        first = build_vp_submit_body(job, model="N1")
        second = build_vp_submit_body(job, model="N1")

        self.assertEqual(first["hubSubmissionUid"], second["hubSubmissionUid"])
        self.assertEqual(len(first["hubSubmissionUid"]), 32)
        self.assertNotIn("plate", first)

    def test_build_vp_submit_body_flattens_dict_ams_mapping(self):
        # A print carrying both ams_mapping + ams_mapping2 stores a dict on the
        # job; the cloud contract is amsMapping: number[], so the body must send
        # the primary list (else every AMS-2-Pro print 400s on the Zod parse).
        job = CapturedJob(
            member_id="member-1",
            filename="part.3mf",
            file_path=Path("part.3mf"),
            sha256="c" * 64,
            size=10,
            ams_mapping={"ams_mapping": [0, 1, 2], "ams_mapping2": {"0": 0}},
            use_ams=True,
            required_filaments=[],
            submitted_at=datetime(2026, 6, 13, tzinfo=timezone.utc),
        )

        body = build_vp_submit_body(job, model="3DPrinter-X1-Carbon")

        self.assertIsInstance(body["amsMapping"], list)
        self.assertEqual(body["amsMapping"], [0, 1, 2])

    def test_parse_project_file_resolves_plate_from_param(self):
        # Bambu often carries the plate only in `param` (Metadata/plate_N.gcode),
        # not a literal `plate` field.
        intent = parse_project_file_command(
            {
                "print": {
                    "command": "project_file",
                    "param": "Metadata/plate_3.gcode",
                    "file": "part.3mf",
                }
            },
            "member-1",
        )

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.plate, 3)

    def test_project_file_parse_and_capture_after_upload(self):
        captured = []
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "part.3mf"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "Metadata/slice_info.config",
                    '<config><metadata key="filament_type" value="PLA"/></config>',
                )
            upload = UploadRecord(
                member_id="m1",
                filename="part.3mf",
                file_path=path,
                sha256="abc123",
                size=path.stat().st_size,
            )
            file_md5 = hashlib.md5(path.read_bytes()).hexdigest()
            intent = parse_project_file_command(
                {
                    "print": {
                        "command": "project_file",
                        "sequence_id": "9",
                        "file": "part.3mf",
                        "ams_mapping": [0, 1],
                        "ams_mapping2": {"0": 0},
                        "use_ams": "true",
                        "md5": file_md5,
                    }
                },
                "m1",
            )
            coordinator = CaptureCoordinator(captured.append, lambda _msg: None)

            self.assertIsNotNone(intent)
            coordinator.record_project_file(intent)
            coordinator.record_upload(upload)

        self.assertEqual(len(captured), 1)
        job = captured[0]
        self.assertEqual(job.member_id, "m1")
        self.assertEqual(job.filename, "part.3mf")
        self.assertTrue(job.use_ams)
        self.assertEqual(job.ams_mapping, {"ams_mapping": [0, 1], "ams_mapping2": {"0": 0}})
        self.assertEqual(job.required_filaments, [{"slot": 0, "material": "PLA"}])

    def test_assemble_rejects_member_mismatch(self):
        upload = UploadRecord("m1", "part.3mf", Path("part.3mf"), "sha", 1)
        intent = ProjectFileIntent("m2", "part.3mf", None, None, False, None, {})

        with self.assertRaises(ValueError):
            assemble_captured_job(upload, intent)

    def test_same_filename_from_different_members_captures_both(self):
        captured = []
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            paths = []
            for idx in range(2):
                path = base / f"spooled-{idx}.3mf"
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("Metadata/slice_info.config", "<config/>")
                paths.append(path)
            coordinator = CaptureCoordinator(captured.append, lambda _msg: None)

            coordinator.record_project_file(
                ProjectFileIntent("m1", "part.3mf", None, None, False, None, {})
            )
            coordinator.record_project_file(
                ProjectFileIntent("m2", "part.3mf", None, None, False, None, {})
            )
            coordinator.record_upload(
                UploadRecord("m2", "part.3mf", paths[1], "sha2", paths[1].stat().st_size)
            )
            coordinator.record_upload(
                UploadRecord("m1", "part.3mf", paths[0], "sha1", paths[0].stat().st_size)
            )

        self.assertEqual({job.member_id for job in captured}, {"m1", "m2"})
        self.assertEqual({job.file_path.name for job in captured}, {"spooled-0.3mf", "spooled-1.3mf"})

    def test_md5_mismatch_skips_capture(self):
        captured = []
        messages = []
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "part.3mf"
            path.write_bytes(b"not a zip")
            coordinator = CaptureCoordinator(captured.append, messages.append)
            coordinator.record_project_file(
                ProjectFileIntent("m1", "part.3mf", None, None, False, "0" * 32, {})
            )
            coordinator.record_upload(
                UploadRecord("m1", "part.3mf", path, "sha", path.stat().st_size)
            )

        self.assertEqual(captured, [])
        self.assertTrue(any("md5" in message for message in messages))

    def test_ambiguous_same_member_same_filename_is_rejected(self):
        captured = []
        messages = []
        with tempfile.TemporaryDirectory() as d:
            first = Path(d) / "first.3mf"
            second = Path(d) / "second.3mf"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            coordinator = CaptureCoordinator(captured.append, messages.append)
            coordinator.record_upload(UploadRecord("m1", "part.3mf", first, "sha1", 5))
            coordinator.record_upload(UploadRecord("m1", "part.3mf", second, "sha2", 6))
            coordinator.record_project_file(
                ProjectFileIntent("m1", "part.3mf", None, None, False, None, {})
            )

        self.assertEqual(captured, [])
        self.assertTrue(any("ambiguous" in message for message in messages))

    def test_pending_capture_state_has_ttl_and_total_caps(self):
        now = [100.0]
        coordinator = CaptureCoordinator(
            lambda _job: None,
            lambda _msg: None,
            upload_wait_sec=10.0,
            max_pending=2,
            clock=lambda: now[0],
        )
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            for idx in range(3):
                path = base / f"upload-{idx}.3mf"
                path.write_bytes(b"data")
                coordinator.record_upload(
                    UploadRecord("m1", f"upload-{idx}.3mf", path, "sha", path.stat().st_size)
                )
                coordinator.record_project_file(
                    ProjectFileIntent("m1", f"intent-{idx}.3mf", None, None, False, None, {})
                )

            self.assertEqual(sum(len(q) for q in coordinator._uploads.values()), 2)
            self.assertEqual(sum(len(q) for q in coordinator._intents.values()), 2)
            now[0] += 11.0
            fresh = base / "fresh.3mf"
            fresh.write_bytes(b"fresh")
            coordinator.record_upload(
                UploadRecord("m1", "fresh.3mf", fresh, "sha", fresh.stat().st_size)
            )

        self.assertEqual(sum(len(q) for q in coordinator._uploads.values()), 1)
        self.assertEqual(sum(len(q) for q in coordinator._intents.values()), 0)


class TestFtpSession(unittest.TestCase):
    def test_pasv_before_login_is_rejected(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                auth = MemberAuthSet([VirtualPrinterMember(_code_hash("12345678"), "m1")])
                session = _ftp_session(FtpConfig("100.64.0.10", Path(d), auth))
                session.open_passive = mock.AsyncMock()

                await session.cmd_pasv()

                self.assertIn(b"530 Not logged in\r\n", session.writer.writes)
                session.open_passive.assert_not_awaited()

        asyncio.run(run())

    def test_same_name_stor_uses_unique_spool_paths_and_original_metadata(self):
        async def run():
            records = []
            with tempfile.TemporaryDirectory() as d:
                auth = MemberAuthSet([VirtualPrinterMember(_code_hash("12345678"), "m1")])
                session = _ftp_session(
                    FtpConfig("100.64.0.10", Path(d), auth, on_stored=records.append)
                )
                session.logged_in = True
                session.member_id = "m1"
                payloads = [b"first", b"second"]

                async def receive_file(_reader, _writer, partial):
                    data = payloads.pop(0)
                    partial.write_bytes(data)
                    return len(data), hashlib.sha256(data).hexdigest()

                session.receive_file = receive_file
                uuids = [uuid.UUID(int=1), uuid.UUID(int=2)]
                with mock.patch("makeros_hub.vprinter.ftp_server.uuid.uuid4", side_effect=uuids):
                    session.passive = _FakePassive()
                    await session.cmd_stor("same.3mf")
                    session.passive = _FakePassive()
                    await session.cmd_stor("same.3mf")

                self.assertEqual([record.filename for record in records], ["same.3mf", "same.3mf"])
                self.assertEqual(len({record.file_path for record in records}), 2)
                self.assertTrue(all(record.file_path.name.endswith("-same.3mf") for record in records))
                self.assertEqual([record.file_path.read_bytes() for record in records], [b"first", b"second"])

        asyncio.run(run())


def _mqtt_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(2, "big") + raw


def _ftp_session(config: FtpConfig, peer=("100.64.0.20", 12345)) -> FtpSession:
    return FtpSession(
        asyncio.StreamReader(),
        _FakeWriter(peer),
        config,
        ssl_context=None,
        log=lambda _msg: None,
    )


class _FakeWriter:
    def __init__(self, peer):
        self.peer = peer
        self.writes = []
        self.closed = False

    def get_extra_info(self, name):
        if name == "peername":
            return self.peer
        return None

    def write(self, data):
        self.writes.append(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed

    async def wait_closed(self):
        return None


class _FakePassive:
    def __init__(self):
        loop = asyncio.get_running_loop()
        self.connection = loop.create_future()
        self.connection.set_result((asyncio.StreamReader(), _FakeWriter(("100.64.0.40", 54321))))
        self.closed = False

    async def close(self):
        self.closed = True


if __name__ == "__main__":
    unittest.main()
