from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import struct
import tempfile
import unittest
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from makeros_hub.config import VirtualPrinterMember, parse_virtual_printer_config
from makeros_hub.vprinter.ftp_server import FtpConfig, FtpServer, FtpSession, sweep_uploads_dir
from makeros_hub.vprinter.auth import AuthRateLimiter, MemberAuthSet
from makeros_hub.vprinter.bind_server import (
    END_MAGIC,
    START_MAGIC,
    decode_frame,
    encode_frame,
    start_bind_server,
)
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
    MqttSession,
    MqttBroker,
    build_publish,
    decode_remaining_length_from_bytes,
    encode_remaining_length,
    parse_connect,
    parse_publish,
    _read_packet,
)
from makeros_hub.vprinter.manager import (
    _AsyncVirtualPrinterSupervisor,
    _VirtualPrinterRuntime,
    _hot_state,
    _identity_fingerprint,
    _pool_signature,
)
from makeros_hub.vprinter.outbox import VPrinterOutbox, from_record
from makeros_hub.vprinter.report import build_get_version, build_print_ack, build_push_status
from makeros_hub.vprinter.ssdp import (
    BambuSsdpProtocol,
    SSDP_RATE_LIMIT_BURST,
    SsdpConfig,
)


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

    def test_hot_state_uses_only_pool_identity_fields(self):
        def make_cfg(**tray_overrides):
            tray = {
                "tray_type": "PLA",
                "tray_info_idx": "GFA00",
                "tray_sub_brands": "MakerOS",
                "tray_color": "FFFFFFFF",
                "cols": ["FFFFFFFF"],
                "remain": 100,
                "nozzle_temp_min": "190",
                "nozzle_temp_max": "230",
                "tag_uid": "tag-1",
                "tray_uuid": "uuid-1",
            }
            tray.update(tray_overrides)
            return parse_virtual_printer_config(
                {
                    "enabled": True,
                    "serial": "SER123",
                    "model": "N1",
                    "name": "VP A1",
                    "fw": "01.08.00.00",
                    "bind_ip": "100.64.0.10",
                    "members": [{"access_code_sha256": _code_hash("12345678"), "member_id": "m1"}],
                    "pool": [tray],
                }
            )

        base = make_cfg()
        volatile_changed = make_cfg(
            remain=42,
            nozzle_temp_min="200",
            nozzle_temp_max="250",
            tag_uid="tag-2",
            tray_uuid="uuid-2",
        )
        identity_changed = make_cfg(
            tray_type="PETG",
            tray_info_idx="GFG00",
            tray_color="11223344",
            cols=["11223344"],
        )

        self.assertIsNotNone(base)
        self.assertIsNotNone(volatile_changed)
        self.assertIsNotNone(identity_changed)
        self.assertEqual(_identity_fingerprint(base), _identity_fingerprint(identity_changed))
        self.assertEqual(_hot_state(base), _hot_state(volatile_changed))
        self.assertNotEqual(_hot_state(base), _hot_state(identity_changed))


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

    def test_push_status_ams_version_is_settable(self):
        # Bambu's Device tab only re-reads the AMS when ams.version increments.
        self.assertEqual(build_push_status()["print"]["ams"]["version"], 4)
        self.assertEqual(
            build_push_status(ams_version=123456)["print"]["ams"]["version"], 123456
        )

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


class TestAmsVersionBump(unittest.TestCase):
    """ams.version must move when the displayed pool changes (Bambu Device-tab
    refresh gate) and start high enough that a restart unsticks a stale display."""

    def _config(self, pool, members=()):
        # The parser requires >=1 valid member; seed one when the test doesn't
        # care about members (the pool is what drives the version).
        members = list(members) or [
            {"access_code_sha256": _code_hash("seedseed"), "member_id": "seed"}
        ]
        return parse_virtual_printer_config(
            {
                "enabled": True,
                "serial": "SER123",
                "model": "N1",
                "name": "VP",
                "fw": "01.08.00.00",
                "bind_ip": "100.64.0.10",
                "members": members,
                "pool": pool,
            }
        )

    def _runtime(self, pool, members=()):
        return _VirtualPrinterRuntime(
            self._config(pool, members),
            base_dir=Path("/tmp/makeros-vp-test"),
            on_capture=lambda *a, **k: None,
        )

    def test_seeded_high_so_a_restart_unsticks_a_stale_display(self):
        rt = self._runtime([{"tray_type": "PLA", "tray_info_idx": "GFA00"}])
        # Seeded from wall-clock -> far above the old hardcoded 4 a client cached,
        # so the first report after a restart always reads as "newer".
        self.assertGreater(rt._ams_version, 4)

    def test_signature_ignores_members_and_volatile_fields(self):
        a = _pool_signature(({"tray_type": "PLA", "tray_info_idx": "GFA00", "remain": 80},))
        b = _pool_signature(({"tray_type": "PLA", "tray_info_idx": "GFA00", "remain": 5},))
        c = _pool_signature(({"tray_type": "ABS", "tray_info_idx": "GFB00"},))
        self.assertEqual(a, b)  # remain% is volatile, not identity
        self.assertNotEqual(a, c)

    def test_bumps_on_pool_change_only(self):
        pla = [{"tray_type": "PLA", "tray_info_idx": "GFA00"}]
        rt = self._runtime(pla)
        v0 = rt._ams_version

        # Member-only change must NOT bump (don't churn the Device tab).
        member = {"access_code_sha256": _code_hash("12345678"), "member_id": "m1"}
        asyncio.run(rt.apply_hot(self._config(pla, members=[member])))
        self.assertEqual(rt._ams_version, v0)

        # Pool change MUST bump, strictly increasing.
        asyncio.run(rt.apply_hot(self._config([{"tray_type": "ABS", "tray_info_idx": "GFB00"}])))
        self.assertGreater(rt._ams_version, v0)
        v1 = rt._ams_version

        # Re-applying the same pool must NOT bump again.
        asyncio.run(rt.apply_hot(self._config([{"tray_type": "ABS", "tray_info_idx": "GFB00"}])))
        self.assertEqual(rt._ams_version, v1)


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

    def test_bind_listener_uses_reuse_address(self):
        async def run():
            with mock.patch("asyncio.start_server", new=mock.AsyncMock(return_value=_FakeServer())) as start:
                server = await start_bind_server(
                    "127.0.0.1",
                    3000,
                    mock.Mock(serial="SER123", model="N1", name="VP", fw="01"),
                    lambda _msg: None,
                )

            self.assertIsInstance(server, _FakeServer)
            self.assertTrue(start.await_args.kwargs["reuse_address"])

        asyncio.run(run())

    def test_bind_connection_cap_refuses_extra_connection(self):
        async def run():
            captured = {}
            first_started = asyncio.Event()
            release_first = asyncio.Event()

            async def fake_start_server(handler, *args, **kwargs):
                captured["handler"] = handler
                return _FakeServer()

            async def fake_read_frame(_reader):
                first_started.set()
                await release_first.wait()
                return {"login": {"command": "detect"}}

            with (
                mock.patch("asyncio.start_server", new=mock.AsyncMock(side_effect=fake_start_server)),
                mock.patch("makeros_hub.vprinter.bind_server.read_frame", side_effect=fake_read_frame),
            ):
                await start_bind_server(
                    "127.0.0.1",
                    3000,
                    mock.Mock(serial="SER123", model="N1", name="VP", fw="01"),
                    lambda _msg: None,
                    max_connections=1,
                )

                writer1 = _FakeWriter(("100.64.0.20", 5000))
                task1 = asyncio.create_task(captured["handler"](asyncio.StreamReader(), writer1))
                await first_started.wait()

                writer2 = _FakeWriter(("100.64.0.21", 5001))
                await captured["handler"](asyncio.StreamReader(), writer2)

                self.assertTrue(writer2.closed)
                self.assertFalse(writer1.closed)
                release_first.set()
                await task1
                self.assertTrue(writer1.closed)

        asyncio.run(run())


class TestSsdpProtocol(unittest.TestCase):
    def test_msearch_replies_and_throttles_bursts_per_source(self):
        config = SsdpConfig(
            ip="100.64.0.10",
            serial="SER123",
            model="N1",
            name="VP",
            fw="01.08.00.00",
        )
        transport = _FakeDatagramTransport()
        protocol = BambuSsdpProtocol(config, lambda _msg: None)
        protocol.connection_made(transport)
        payload = (
            b"M-SEARCH * HTTP/1.1\r\n"
            b"HOST: 239.255.255.250:1900\r\n"
            b'MAN: "ssdp:discover"\r\n'
            b"MX: 1\r\n\r\n"
        )

        for _ in range(SSDP_RATE_LIMIT_BURST + 2):
            protocol.datagram_received(payload, ("100.64.0.20", 1900))

        self.assertEqual(len(transport.sent), SSDP_RATE_LIMIT_BURST)
        self.assertTrue(transport.sent[0][0].startswith(b"HTTP/1.1 200 OK"))

    def test_short_msearch_datagram_is_ignored(self):
        config = SsdpConfig(
            ip="100.64.0.10",
            serial="SER123",
            model="N1",
            name="VP",
            fw="01.08.00.00",
        )
        transport = _FakeDatagramTransport()
        protocol = BambuSsdpProtocol(config, lambda _msg: None)
        protocol.connection_made(transport)

        protocol.datagram_received(b"M-SEARCH", ("100.64.0.20", 1900))

        self.assertEqual(transport.sent, [])


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

    def test_mqtt_listener_uses_reuse_address(self):
        async def run():
            broker = _mqtt_broker()
            with mock.patch("asyncio.start_server", new=mock.AsyncMock(return_value=_FakeServer())) as start:
                server = await broker.start("127.0.0.1", 8883, ssl_context=None)

            self.assertIsInstance(server, _FakeServer)
            self.assertTrue(start.await_args.kwargs["reuse_address"])

        asyncio.run(run())

    def test_silent_client_times_out_and_cleans_active_writer(self):
        async def run():
            broker = _mqtt_broker()
            reader = asyncio.StreamReader()
            payload = (
                _mqtt_string("MQTT")
                + bytes([4, 0xC2])
                + (1).to_bytes(2, "big")
                + _mqtt_string("orca-client")
                + _mqtt_string("bblp")
                + _mqtt_string("12345678")
            )
            reader.feed_data(b"\x10" + encode_remaining_length(len(payload)) + payload)
            writer = _FakeWriter(("100.64.0.20", 1883), sock=_FakeSocket())

            with mock.patch("makeros_hub.vprinter.mqtt_broker._mqtt_read_timeout", return_value=0.01):
                await broker._handle_client(reader, writer)

            self.assertTrue(writer.closed)
            self.assertIsNone(broker._active_writer)
            self.assertIsNone(broker._active_session)
            self.assertEqual(broker._client_tasks, set())
            self.assertIn(
                (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
                writer.sock.options,
            )

        asyncio.run(run())

    def test_reset_print_state_clears_lingering_job(self):
        broker = _mqtt_broker()
        broker.set_print_state("FINISH", gcode_file="Cube.gcode.3mf", prepare_percent="100")
        broker._reset_print_state()
        self.assertEqual(broker._gcode_state, "IDLE")
        self.assertEqual(broker._gcode_file, "")
        self.assertEqual(broker._prepare_percent, "0")
        self.assertIsNone(broker._finish_task)

    def test_fresh_connect_presents_idle_not_a_phantom_job(self):
        async def run():
            broker = _mqtt_broker()
            # A prior session left a completed job in the broker's print state.
            broker.set_print_state("FINISH", gcode_file="Cube.gcode.3mf", prepare_percent="100")
            reader = asyncio.StreamReader()
            payload = (
                _mqtt_string("MQTT")
                + bytes([4, 0xC2])
                + (1).to_bytes(2, "big")
                + _mqtt_string("orca-client")
                + _mqtt_string("bblp")
                + _mqtt_string("12345678")
            )
            reader.feed_data(b"\x10" + encode_remaining_length(len(payload)) + payload)
            writer = _FakeWriter(("100.64.0.20", 1883), sock=_FakeSocket())
            with mock.patch("makeros_hub.vprinter.mqtt_broker._mqtt_read_timeout", return_value=0.01):
                await broker._handle_client(reader, writer)
            # The fresh connect cleared the phantom job -> a clean idle printer.
            self.assertEqual(broker._gcode_state, "IDLE")
            self.assertEqual(broker._gcode_file, "")

        asyncio.run(run())

    def test_push_report_now_writes_active_session_and_noops_without_one(self):
        async def run():
            broker = _mqtt_broker()

            await broker.push_report_now()

            writer = _FakeWriter(("100.64.0.20", 1883))
            broker._active_session = MqttSession(
                writer=writer,
                peer=writer.peer,
                member_id="m1",
                report_topic=broker.default_report_topic,
                request_topic=broker.default_request_topic,
                subscribed=True,
            )

            await broker.push_report_now()

            self.assertEqual(len(writer.writes), 1)
            remaining, consumed = decode_remaining_length_from_bytes(writer.writes[0][1:])
            publish = parse_publish(
                writer.writes[0][0],
                writer.writes[0][1 + consumed : 1 + consumed + remaining],
            )
            self.assertEqual(publish.topic, "device/SER123/report")
            self.assertEqual(json.loads(publish.payload)["print"]["command"], "push_status")

            writer.close()
            await broker.push_report_now()
            self.assertEqual(len(writer.writes), 1)

        asyncio.run(run())

    def test_displaced_session_publish_cannot_mutate_print_state(self):
        async def run():
            captured = []
            broker = _mqtt_broker(on_project_file=captured.append)
            old_writer = _FakeWriter(("100.64.0.20", 1883))
            new_writer = _FakeWriter(("100.64.0.21", 1883))
            old_session = MqttSession(
                writer=old_writer,
                peer=old_writer.peer,
                member_id="old-member",
                report_topic=broker.default_report_topic,
                request_topic=broker.default_request_topic,
            )
            new_session = MqttSession(
                writer=new_writer,
                peer=new_writer.peer,
                member_id="new-member",
                report_topic=broker.default_report_topic,
                request_topic=broker.default_request_topic,
            )
            broker._active_session = new_session
            broker.set_print_state("PREPARE", gcode_file="active.3mf", prepare_percent="42")
            finish_task = asyncio.create_task(asyncio.sleep(60))
            broker._finish_task = finish_task
            publish = parse_publish(
                0x30,
                _mqtt_string(broker.default_request_topic)
                + json.dumps(
                    {
                        "print": {
                            "command": "project_file",
                            "sequence_id": "9",
                            "file": "stale.3mf",
                        }
                    }
                ).encode("utf-8"),
            )

            await broker._handle_publish(old_session, publish)

            self.assertEqual(broker._gcode_state, "PREPARE")
            self.assertEqual(broker._gcode_file, "active.3mf")
            self.assertEqual(broker._prepare_percent, "42")
            self.assertIs(broker._finish_task, finish_task)
            self.assertFalse(finish_task.done())
            self.assertEqual(old_writer.writes, [])
            self.assertEqual(captured, [])
            finish_task.cancel()
            await asyncio.gather(finish_task, return_exceptions=True)

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

    def test_rate_limited_auth_does_not_self_extend_lockout(self):
        now = [1000.0]
        limiter = AuthRateLimiter(limit=2, window_sec=10.0, clock=lambda: now[0])
        auth = MemberAuthSet([VirtualPrinterMember(_code_hash("12345678"), "m1")], limiter=limiter)

        self.assertFalse(auth.authenticate("badcode", "100.64.0.20").rate_limited)
        self.assertFalse(auth.authenticate("badcode", "100.64.0.20").rate_limited)
        for _ in range(20):
            now[0] += 0.25
            self.assertTrue(auth.authenticate("badcode", "100.64.0.20").rate_limited)

        now[0] = 1010.1
        result = auth.authenticate("12345678", "100.64.0.20")

        self.assertTrue(result.ok)
        self.assertEqual(result.member_id, "m1")

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

    def test_replace_members_swaps_codes_and_preserves_limiter(self):
        limiter = AuthRateLimiter()
        auth = MemberAuthSet([VirtualPrinterMember(_code_hash("12345678"), "m1")], limiter=limiter)

        self.assertTrue(auth.authenticate("12345678", "100.64.0.20").ok)

        auth.replace_members([VirtualPrinterMember(_code_hash("87654321"), "m2")])

        self.assertIs(auth.limiter, limiter)
        self.assertFalse(auth.authenticate("12345678", "100.64.0.20").ok)
        result = auth.authenticate("87654321", "100.64.0.20")
        self.assertTrue(result.ok)
        self.assertEqual(result.member_id, "m2")

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
    def test_assemble_submission_uid_is_deterministic_for_identical_logical_prints(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "part.3mf"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Metadata/slice_info.config", "<config/>")
            file_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            upload = UploadRecord("member-1", "part.3mf", path, file_sha256, path.stat().st_size)
            list_intent = ProjectFileIntent(
                "member-1",
                "part.3mf",
                [0, 1],
                None,
                True,
                None,
                {},
                plate=2,
            )
            dict_intent = ProjectFileIntent(
                "member-1",
                "part.3mf",
                [0, 1],
                {"0": 0},
                True,
                None,
                {},
                plate=2,
            )

            first = assemble_captured_job(upload, list_intent)
            second = assemble_captured_job(upload, list_intent)
            dict_form = assemble_captured_job(upload, dict_intent)

        expected = hashlib.sha256(
            f"member-1\n{file_sha256}\n2\n[0,1]".encode()
        ).hexdigest()
        self.assertEqual(first.submission_uid, expected)
        self.assertEqual(first.submission_uid, second.submission_uid)
        self.assertEqual(first.submission_uid, dict_form.submission_uid)
        self.assertEqual(len(first.submission_uid), 64)
        self.assertEqual(first.submission_uid, first.submission_uid.lower())

    def test_assemble_submission_uid_changes_for_logical_print_inputs(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "part.3mf"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Metadata/slice_info.config", "<config/>")
            size = path.stat().st_size
            base_upload = UploadRecord("member-1", "part.3mf", path, "a" * 64, size)
            base_intent = ProjectFileIntent(
                "member-1",
                "part.3mf",
                [0, 1],
                None,
                True,
                None,
                {},
                plate=2,
            )
            base = assemble_captured_job(base_upload, base_intent).submission_uid
            changed_file = assemble_captured_job(
                UploadRecord("member-1", "part.3mf", path, "b" * 64, size),
                base_intent,
            ).submission_uid
            changed_member = assemble_captured_job(
                UploadRecord("member-2", "part.3mf", path, "a" * 64, size),
                ProjectFileIntent(
                    "member-2",
                    "part.3mf",
                    [0, 1],
                    None,
                    True,
                    None,
                    {},
                    plate=2,
                ),
            ).submission_uid
            changed_plate = assemble_captured_job(
                base_upload,
                ProjectFileIntent(
                    "member-1",
                    "part.3mf",
                    [0, 1],
                    None,
                    True,
                    None,
                    {},
                    plate=3,
                ),
            ).submission_uid
            changed_mapping = assemble_captured_job(
                base_upload,
                ProjectFileIntent(
                    "member-1",
                    "part.3mf",
                    [1, 0],
                    None,
                    True,
                    None,
                    {},
                    plate=2,
                ),
            ).submission_uid

        self.assertNotEqual(base, changed_file)
        self.assertNotEqual(base, changed_member)
        self.assertNotEqual(base, changed_plate)
        self.assertNotEqual(base, changed_mapping)

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


class TestVPrinterOutbox(unittest.TestCase):
    def test_persist_load_round_trips_job_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            outbox = VPrinterOutbox(Path(d) / "vp-outbox")
            job = CapturedJob(
                member_id="member-1",
                filename="part.3mf",
                file_path=Path(d) / "part.3mf",
                sha256="a" * 64,
                size=123,
                ams_mapping={"ams_mapping": [0], "ams_mapping2": {"0": 0}},
                use_ams=True,
                required_filaments=[{"slot": 0, "material": "PLA"}],
                submitted_at=datetime(2026, 6, 13, 12, 30, tzinfo=timezone.utc),
                submission_uid="0" * 32,
                plate=2,
                attempts=3,
            )

            outbox.persist(job)
            loaded = outbox.load_all()

            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0], job)
            final_path = Path(d) / "vp-outbox" / f"{job.submission_uid}.json"
            self.assertTrue(final_path.exists())
            self.assertEqual(list((Path(d) / "vp-outbox").glob("*.tmp")), [])
            record = json.loads(final_path.read_text(encoding="utf-8"))
            self.assertEqual(record["submission_uid"], job.submission_uid)

    def test_corrupt_record_is_quarantined_without_crashing_load(self):
        with tempfile.TemporaryDirectory() as d:
            outbox_dir = Path(d) / "vp-outbox"
            outbox_dir.mkdir()
            corrupt = outbox_dir / ("1" * 32 + ".json")
            corrupt.write_text("{not json", encoding="utf-8")
            outbox = VPrinterOutbox(outbox_dir)

            with self.assertLogs("makeros-hub.vprinter.outbox", level="WARNING"):
                loaded = outbox.load_all()

            self.assertEqual(loaded, [])
            self.assertFalse(corrupt.exists())
            self.assertTrue((outbox_dir / (corrupt.name + ".corrupt")).exists())

    def test_record_loader_tolerates_missing_and_extra_fields(self):
        job = from_record(
            {
                "memberId": "member-2",
                "fileName": "older.3mf",
                "fileSha256": "b" * 64,
                "fileSizeBytes": "456",
                "useAms": "true",
                "submittedAt": "2026-06-13T12:30:00Z",
                "unknownFutureField": {"ok": True},
            }
        )

        self.assertEqual(job.member_id, "member-2")
        self.assertEqual(job.filename, "older.3mf")
        self.assertEqual(job.file_path, Path("older.3mf"))
        self.assertEqual(job.sha256, "b" * 64)
        self.assertEqual(job.size, 456)
        self.assertEqual(job.ams_mapping, [])
        self.assertTrue(job.use_ams)
        self.assertEqual(job.required_filaments, [])
        self.assertEqual(job.attempts, 0)
        self.assertEqual(len(job.submission_uid), 32)

    def test_submission_uid_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            outbox = VPrinterOutbox(Path(d) / "vp-outbox")
            job = CapturedJob(
                member_id="member-1",
                filename="part.3mf",
                file_path=Path("part.3mf"),
                sha256="a" * 64,
                size=123,
                ams_mapping=[],
                use_ams=False,
                required_filaments=[],
                submitted_at=datetime(2026, 6, 13, tzinfo=timezone.utc),
                submission_uid="../escape",
            )

            with self.assertRaises(ValueError):
                outbox.persist(job)
            with self.assertRaises(ValueError):
                outbox.remove("../escape")


class TestFtpSession(unittest.TestCase):
    def test_sweep_uploads_removes_old_and_cap_excess_but_keeps_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            now = 10_000.0
            old_committed = _write_upload(base / "old.3mf", b"old", now - 90_000)
            old_part = _write_upload(base / ".old.3mf.123.part", b"part", now - 600)
            cap_old_1 = _write_upload(base / "cap-old-1.3mf", b"a" * 60, now - 1_000)
            cap_old_2 = _write_upload(base / "cap-old-2.3mf", b"b" * 60, now - 900)
            fresh = _write_upload(base / "fresh.3mf", b"fresh" * 16, now)

            result = sweep_uploads_dir(
                base,
                committed_ttl_sec=24 * 60 * 60,
                part_ttl_sec=300,
                max_total_bytes=100,
                now=now,
            )

            self.assertFalse(old_committed.exists())
            self.assertFalse(old_part.exists())
            self.assertFalse(cap_old_1.exists())
            self.assertFalse(cap_old_2.exists())
            self.assertTrue(fresh.exists())
            self.assertEqual(result["removed_age"], 2)
            self.assertEqual(result["removed_cap"], 2)

    def test_ftp_control_listener_uses_reuse_address(self):
        async def run():
            auth = MemberAuthSet([VirtualPrinterMember(_code_hash("12345678"), "m1")])
            with tempfile.TemporaryDirectory() as d:
                server = FtpServer(
                    "127.0.0.1",
                    990,
                    FtpConfig("100.64.0.10", Path(d), auth),
                    ssl_context=None,
                    log=lambda _msg: None,
                )
                with mock.patch("asyncio.start_server", new=mock.AsyncMock(return_value=_FakeServer())) as start:
                    returned = await server.start()

            self.assertIs(returned, server)
            self.assertTrue(start.await_args.kwargs["reuse_address"])

        asyncio.run(run())

    def test_ftp_session_cap_refuses_extra_control_connection(self):
        async def run():
            first_started = asyncio.Event()
            release_first = asyncio.Event()

            class BlockingSession:
                def __init__(self, reader, writer, config, ssl_context, log):
                    self.writer = writer

                async def run(self):
                    first_started.set()
                    await release_first.wait()

                async def abort(self):
                    self.writer.close()

            with tempfile.TemporaryDirectory() as d:
                auth = MemberAuthSet([VirtualPrinterMember(_code_hash("12345678"), "m1")])
                server = FtpServer(
                    "127.0.0.1",
                    990,
                    FtpConfig("100.64.0.10", Path(d), auth, max_sessions=1),
                    ssl_context=None,
                    log=lambda _msg: None,
                )
                with mock.patch("makeros_hub.vprinter.ftp_server.FtpSession", BlockingSession):
                    writer1 = _FakeWriter(("100.64.0.20", 5000))
                    task1 = asyncio.create_task(server._handle(asyncio.StreamReader(), writer1))
                    await first_started.wait()

                    writer2 = _FakeWriter(("100.64.0.21", 5001))
                    await server._handle(asyncio.StreamReader(), writer2)

                    self.assertTrue(writer2.closed)
                    self.assertFalse(writer1.closed)
                    release_first.set()
                    await task1

        asyncio.run(run())

    def test_ftp_passive_listener_uses_reuse_address(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                auth = MemberAuthSet([VirtualPrinterMember(_code_hash("12345678"), "m1")])
                session = _ftp_session(
                    FtpConfig("100.64.0.10", Path(d), auth, passive_start=50000, passive_end=50000)
                )
                with mock.patch("asyncio.start_server", new=mock.AsyncMock(return_value=_FakeServer())) as start:
                    passive = await session.open_passive()

                self.assertEqual(passive.port, 50000)
                self.assertTrue(start.await_args.kwargs["reuse_address"])
                await passive.close()

        asyncio.run(run())

    def test_ftp_passive_rejects_data_peer_mismatch_and_accepts_control_peer(self):
        async def run():
            captured = {}

            async def fake_start_server(handler, *args, **kwargs):
                captured["handler"] = handler
                return _FakeServer()

            with tempfile.TemporaryDirectory() as d:
                auth = MemberAuthSet([VirtualPrinterMember(_code_hash("12345678"), "m1")])
                session = _ftp_session(
                    FtpConfig("100.64.0.10", Path(d), auth, passive_start=50000, passive_end=50000),
                    peer=("100.64.0.20", 12345),
                )
                with mock.patch("asyncio.start_server", new=mock.AsyncMock(side_effect=fake_start_server)):
                    passive = await session.open_passive()

                bad_writer = _FakeWriter(("100.64.0.99", 5001))
                await captured["handler"](asyncio.StreamReader(), bad_writer)
                self.assertTrue(bad_writer.closed)
                self.assertFalse(passive.connection.done())

                good_reader = asyncio.StreamReader()
                good_writer = _FakeWriter(("100.64.0.20", 5002))
                task = asyncio.create_task(captured["handler"](good_reader, good_writer))
                reader, writer = await asyncio.wait_for(passive.connection, timeout=0.1)

                self.assertIs(reader, good_reader)
                self.assertIs(writer, good_writer)
                self.assertFalse(good_writer.closed)
                await passive.close()
                await task

        asyncio.run(run())

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


class TestVirtualPrinterSupervisor(unittest.TestCase):
    def test_concurrent_reconciles_do_not_interleave(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                first_started = asyncio.Event()
                release_first = asyncio.Event()
                events = []

                class FakeRuntime:
                    def __init__(self, config, *, base_dir, on_capture):
                        self.config = config
                        events.append(("init", config.serial))

                    async def start(self):
                        events.append(("start", self.config.serial))
                        if self.config.serial == "SER1":
                            first_started.set()
                            await release_first.wait()
                        events.append(("started", self.config.serial))

                    async def stop(self):
                        events.append(("stop", self.config.serial))

                supervisor = _AsyncVirtualPrinterSupervisor(
                    base_dir=Path(d),
                    on_capture=lambda _job: None,
                )
                cfg1 = _vp_config("SER1")
                cfg2 = _vp_config("SER2")

                with mock.patch("makeros_hub.vprinter.manager._VirtualPrinterRuntime", FakeRuntime):
                    task1 = asyncio.create_task(supervisor.reconcile(cfg1))
                    await first_started.wait()
                    task2 = asyncio.create_task(supervisor.reconcile(cfg2))
                    await asyncio.sleep(0)
                    self.assertNotIn(("init", "SER2"), events)
                    release_first.set()
                    await asyncio.gather(task1, task2)

            self.assertEqual(
                events,
                [
                    ("init", "SER1"),
                    ("start", "SER1"),
                    ("started", "SER1"),
                    ("stop", "SER1"),
                    ("init", "SER2"),
                    ("start", "SER2"),
                    ("started", "SER2"),
                ],
            )

        asyncio.run(run())

    def test_hot_change_applies_without_runtime_restart(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                instances = []

                class FakeBroker:
                    def __init__(self, auth):
                        self.auth = auth
                        self.pushes = 0

                    async def push_report_now(self):
                        self.pushes += 1

                class FakeRuntime:
                    def __init__(self, config, *, base_dir, on_capture):
                        self.config = config
                        self.auth = MemberAuthSet(config.members)
                        self.broker = FakeBroker(self.auth)
                        self.servers = [object()]
                        self.starts = 0
                        self.stops = 0
                        self.hot_applies = 0
                        instances.append(self)

                    async def start(self):
                        self.starts += 1

                    async def stop(self):
                        self.stops += 1

                    async def apply_hot(self, config):
                        self.hot_applies += 1
                        self.config = config
                        self.auth.replace_members(config.members)
                        await self.broker.push_report_now()

                supervisor = _AsyncVirtualPrinterSupervisor(
                    base_dir=Path(d),
                    on_capture=lambda _job: None,
                )
                cfg1 = _vp_config(
                    "SER1",
                    pool=[{"tray_type": "PLA", "tray_info_idx": "GFA00", "tray_color": "FFFFFFFF"}],
                )
                cfg2 = _vp_config(
                    "SER1",
                    members=[{"access_code_sha256": _code_hash("87654321"), "member_id": "m2"}],
                    pool=[{"tray_type": "PETG", "tray_info_idx": "GFG00", "tray_color": "11223344"}],
                )

                with mock.patch("makeros_hub.vprinter.manager._VirtualPrinterRuntime", FakeRuntime):
                    await supervisor.reconcile(cfg1)
                    runtime = supervisor.runtime
                    self.assertIsNotNone(runtime)
                    assert runtime is not None
                    servers = runtime.servers
                    broker = runtime.broker

                    await supervisor.reconcile(cfg2)

                self.assertIs(supervisor.runtime, runtime)
                self.assertIs(runtime.servers, servers)
                self.assertIs(runtime.broker, broker)
                self.assertEqual(len(instances), 1)
                self.assertEqual(runtime.starts, 1)
                self.assertEqual(runtime.stops, 0)
                self.assertEqual(runtime.hot_applies, 1)
                self.assertEqual(broker.pushes, 1)
                self.assertEqual(broker.auth.members, cfg2.members)

        asyncio.run(run())

    def test_identity_change_restarts_runtime(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                instances = []

                class FakeRuntime:
                    def __init__(self, config, *, base_dir, on_capture):
                        self.config = config
                        self.starts = 0
                        self.stops = 0
                        instances.append(self)

                    async def start(self):
                        self.starts += 1

                    async def stop(self):
                        self.stops += 1

                    async def apply_hot(self, config):
                        self.config = config

                supervisor = _AsyncVirtualPrinterSupervisor(
                    base_dir=Path(d),
                    on_capture=lambda _job: None,
                )
                cfg1 = _vp_config("SER1")
                cfg2 = _vp_config("SER1", bind_ip="100.64.0.11")

                with mock.patch("makeros_hub.vprinter.manager._VirtualPrinterRuntime", FakeRuntime):
                    await supervisor.reconcile(cfg1)
                    first = supervisor.runtime
                    await supervisor.reconcile(cfg2)
                    second = supervisor.runtime

                self.assertIsNot(first, second)
                self.assertEqual(len(instances), 2)
                self.assertEqual(instances[0].starts, 1)
                self.assertEqual(instances[0].stops, 1)
                self.assertEqual(instances[1].starts, 1)
                self.assertEqual(instances[1].stops, 0)

        asyncio.run(run())

    def test_apply_hot_failure_falls_back_to_full_restart(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                instances = []
                diagnostics = mock.Mock()

                class FakeRuntime:
                    def __init__(self, config, *, base_dir, on_capture):
                        self.config = config
                        self.starts = 0
                        self.stops = 0
                        self.hot_applies = 0
                        instances.append(self)

                    async def start(self):
                        self.starts += 1

                    async def stop(self):
                        self.stops += 1

                    async def apply_hot(self, config):
                        self.hot_applies += 1
                        raise RuntimeError("hot boom")

                supervisor = _AsyncVirtualPrinterSupervisor(
                    base_dir=Path(d),
                    on_capture=lambda _job: None,
                    diagnostics=diagnostics,
                )
                cfg1 = _vp_config("SER1")
                cfg2 = _vp_config(
                    "SER1",
                    members=[{"access_code_sha256": _code_hash("87654321"), "member_id": "m2"}],
                )

                with mock.patch("makeros_hub.vprinter.manager._VirtualPrinterRuntime", FakeRuntime):
                    await supervisor.reconcile(cfg1)
                    first = supervisor.runtime
                    await supervisor.reconcile(cfg2)
                    second = supervisor.runtime

                self.assertIsNot(first, second)
                self.assertEqual(len(instances), 2)
                self.assertEqual(instances[0].hot_applies, 1)
                self.assertEqual(instances[0].stops, 1)
                self.assertEqual(instances[1].starts, 1)
                diagnostics.record.assert_called_with("vprinter", "virtual printer hot-apply failed: hot boom")

        asyncio.run(run())


def _mqtt_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(2, "big") + raw


def _mqtt_broker(on_project_file=None) -> MqttBroker:
    auth = MemberAuthSet([VirtualPrinterMember(_code_hash("12345678"), "m1")])
    return MqttBroker(
        serial="SER123",
        auth=auth,
        report_builder=lambda *_args: {"print": {"command": "push_status"}},
        version_builder=lambda _seq: {"info": {"module": []}},
        ack_builder=lambda _seq, _file: {"print": {"command": "project_file"}},
        on_project_file=on_project_file,
        log=lambda _msg: None,
    )


def _vp_config(serial: str, **overrides):
    raw = {
        "enabled": True,
        "serial": serial,
        "model": "N1",
        "name": "VP A1",
        "fw": "01.08.00.00",
        "bind_ip": "100.64.0.10",
        "members": [{"access_code_sha256": _code_hash("12345678"), "member_id": "m1"}],
    }
    raw.update(overrides)
    cfg = parse_virtual_printer_config(raw)
    assert cfg is not None
    return cfg


def _write_upload(path: Path, data: bytes, mtime: float) -> Path:
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def _ftp_session(config: FtpConfig, peer=("100.64.0.20", 12345)) -> FtpSession:
    return FtpSession(
        asyncio.StreamReader(),
        _FakeWriter(peer),
        config,
        ssl_context=None,
        log=lambda _msg: None,
    )


class _FakeWriter:
    def __init__(self, peer, sock=None):
        self.peer = peer
        self.sock = sock
        self.writes = []
        self.closed = False

    def get_extra_info(self, name):
        if name == "peername":
            return self.peer
        if name == "socket":
            return self.sock
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


class _FakeSocket:
    def __init__(self):
        self.options = []

    def setsockopt(self, level, optname, value):
        self.options.append((level, optname, value))


class _FakeDatagramTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))


class _FakeServer:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

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
