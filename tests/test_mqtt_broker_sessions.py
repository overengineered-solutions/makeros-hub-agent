"""RC8 — the Virtual Printer MQTT broker serves MANY OrcaSlicer sessions at once: several members (or one
member's two slicers) on the same VP, each with its own fake print state, none displacing another."""
import asyncio
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from makeros_hub.config import VirtualPrinterMember
from makeros_hub.vprinter import mqtt_broker
from makeros_hub.vprinter.auth import MemberAuthSet
from makeros_hub.vprinter.capture import CaptureCoordinator, UploadRecord
from makeros_hub.vprinter.mqtt_broker import (
    MqttBroker,
    decode_remaining_length_from_bytes,
    encode_remaining_length,
    parse_publish,
)

try:
    from tests.test_vprinter import _code_hash, _FakeSocket, _FakeWriter, _mqtt_string
except ImportError:  # pytest rootdir-style import (no tests/__init__.py)
    from test_vprinter import _code_hash, _FakeSocket, _FakeWriter, _mqtt_string

CODES = {"m1": "12345678", "m2": "87654321"}


def _broker(on_project_file=None) -> MqttBroker:
    auth = MemberAuthSet([VirtualPrinterMember(_code_hash(code), member) for member, code in CODES.items()])
    return MqttBroker(
        serial="SER123",
        auth=auth,
        # echo the per-session state so a report says whose print it is describing
        report_builder=lambda _seq, state, gfile, prep: {
            "print": {"command": "push_status", "gcode_state": state, "gcode_file": gfile, "gcode_file_prepare_percent": prep}
        },
        version_builder=lambda _seq: {"info": {"module": []}},
        ack_builder=lambda _seq, _file: {"print": {"command": "project_file", "result": "SUCCESS"}},
        on_project_file=on_project_file,
        log=lambda _msg: None,
    )


def _connect_frame(code: str) -> bytes:
    payload = (
        _mqtt_string("MQTT") + bytes([4, 0xC2]) + (1).to_bytes(2, "big")
        + _mqtt_string("orca-client") + _mqtt_string("bblp") + _mqtt_string(code)
    )
    return b"\x10" + encode_remaining_length(len(payload)) + payload


def _publish_frame(topic: str, body: dict) -> bytes:
    payload = _mqtt_string(topic) + json.dumps(body).encode("utf-8")
    return b"\x30" + encode_remaining_length(len(payload)) + payload


def _project_file(filename: str, seq: str = "1") -> dict:
    return {"print": {"command": "project_file", "sequence_id": seq, "file": filename,
                      "param": "Metadata/plate_1.gcode", "use_ams": True, "ams_mapping": [0]}}


def _published(writer) -> list[dict]:
    """Every broker->client PUBLISH payload written to this writer, decoded."""
    out = []
    for frame in writer.writes:
        if frame[0] & 0xF0 != 0x30:
            continue
        remaining, consumed = decode_remaining_length_from_bytes(frame[1:])
        out.append(json.loads(parse_publish(frame[0], frame[1 + consumed : 1 + consumed + remaining]).payload))
    return out


class _Client:
    """One OrcaSlicer: a reader we feed, a fake writer we inspect, the broker's handler task."""

    def __init__(self, broker: MqttBroker, member: str, port: int):
        self.reader = asyncio.StreamReader()
        self.writer = _FakeWriter(("100.64.0.2", port), sock=_FakeSocket())
        self.reader.feed_data(_connect_frame(CODES[member]))
        self.task = asyncio.create_task(broker._handle_client(self.reader, self.writer))

    async def settle(self, broker: MqttBroker, ticks: int = 20):
        for _ in range(ticks):
            await asyncio.sleep(0)
            if any(s.writer is self.writer for s in broker._sessions):
                return
        raise AssertionError("session did not come up")

    def session(self, broker: MqttBroker):
        return next(s for s in broker._sessions if s.writer is self.writer)

    async def send(self, body: dict, ticks: int = 20):
        self.reader.feed_data(_publish_frame("device/SER123/request", body))
        for _ in range(ticks):
            await asyncio.sleep(0)

    async def disconnect(self):
        self.reader.feed_data(b"\xE0\x00")
        await self.task


class TestMultiSession(unittest.TestCase):
    def test_second_connection_keeps_the_first_and_pushes_reach_both(self):
        async def run():
            broker = _broker()
            a = _Client(broker, "m1", 5001)
            await a.settle(broker)
            b = _Client(broker, "m2", 5002)
            await b.settle(broker)
            self.assertEqual({s.member_id for s in broker._sessions}, {"m1", "m2"})
            self.assertFalse(a.writer.closed)  # no displacement
            before = (len(_published(a.writer)), len(_published(b.writer)))
            await broker.push_report_now()
            self.assertEqual((len(_published(a.writer)), len(_published(b.writer))), (before[0] + 1, before[1] + 1))
            # one member, two slicers: both stay too
            c = _Client(broker, "m1", 5003)
            await c.settle(broker)
            self.assertEqual(len(broker._sessions), 3)
            for client in (a, b, c):
                await client.disconnect()
            self.assertEqual(broker._sessions, set())
            self.assertTrue(a.writer.closed and b.writer.closed and c.writer.closed)

        asyncio.run(run())

    def test_print_state_is_per_session(self):
        async def run():
            broker = _broker()
            a = _Client(broker, "m1", 5001)
            b = _Client(broker, "m2", 5002)
            await a.settle(broker)
            await b.settle(broker)
            with mock.patch.object(mqtt_broker, "FINISH_DELAY_SEC", 0.01):
                await a.send(_project_file("alice.3mf"))
                await broker.push_report_now()
                self.assertEqual(_published(a.writer)[-1]["print"]["gcode_state"], "PREPARE")
                self.assertEqual(_published(a.writer)[-1]["print"]["gcode_file"], "alice.3mf")
                self.assertEqual(_published(b.writer)[-1]["print"]["gcode_state"], "IDLE")  # Bob's tab untouched
                await asyncio.sleep(0.05)
                await broker.push_report_now()
                self.assertEqual(_published(a.writer)[-1]["print"]["gcode_state"], "FINISH")
                self.assertEqual(_published(b.writer)[-1]["print"]["gcode_state"], "IDLE")
            # the FTPS path targets the uploading MEMBER's sessions only
            broker.set_print_state("FINISH", gcode_file="bob.3mf", prepare_percent="100", member_id="m2")
            self.assertEqual((b.session(broker).gcode_file, a.session(broker).gcode_file), ("bob.3mf", "alice.3mf"))
            await a.disconnect()
            await b.disconnect()

        asyncio.run(run())

    def test_two_members_sends_capture_two_attributed_jobs(self):
        async def run():
            captured = []
            with tempfile.TemporaryDirectory() as d:
                coordinator = CaptureCoordinator(captured.append, lambda _msg: None)
                broker = _broker(on_project_file=coordinator.record_project_file)
                a = _Client(broker, "m1", 5001)
                b = _Client(broker, "m2", 5002)
                await a.settle(broker)
                await b.settle(broker)
                paths = {}
                for member in ("m1", "m2"):
                    path = Path(d) / f"{member}-part.3mf"
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr("Metadata/slice_info.config", "<config/>")
                    paths[member] = path
                # same filename from both members, commands interleaved with uploads
                await a.send(_project_file("part.3mf", seq="7"))
                coordinator.record_upload(UploadRecord("m2", "part.3mf", paths["m2"], "sha2", paths["m2"].stat().st_size))
                await b.send(_project_file("part.3mf", seq="3"))
                coordinator.record_upload(UploadRecord("m1", "part.3mf", paths["m1"], "sha1", paths["m1"].stat().st_size))
                self.assertEqual(sorted(job.member_id for job in captured), ["m1", "m2"])
                self.assertEqual({job.file_path.name for job in captured}, {"m1-part.3mf", "m2-part.3mf"})
                self.assertEqual(len({job.submission_uid for job in captured}), 2)
                self.assertEqual(_published(a.writer)[-1]["print"]["command"], "project_file")  # each got its ack
                self.assertEqual(_published(b.writer)[-1]["print"]["command"], "project_file")
                await a.disconnect()
                await b.disconnect()

        asyncio.run(run())

    def test_closed_session_publish_is_dropped_safely(self):
        async def run():
            captured = []
            broker = _broker(on_project_file=captured.append)
            a = _Client(broker, "m1", 5001)
            b = _Client(broker, "m2", 5002)
            await a.settle(broker)
            await b.settle(broker)
            gone = a.session(broker)
            await a.disconnect()
            writes_before = len(a.writer.writes)
            publish = parse_publish(0x30, _mqtt_string("device/SER123/request") + json.dumps(_project_file("stale.3mf")).encode())
            await broker._handle_publish(gone, publish)  # no exception, nothing captured, nothing written
            self.assertEqual(captured, [])
            self.assertEqual(len(a.writer.writes), writes_before)
            broker.set_print_state("PREPARE", gcode_file="x.3mf", prepare_percent="1", member_id="m1")
            self.assertEqual(gone.gcode_state, "IDLE")
            self.assertEqual(b.session(broker).gcode_state, "IDLE")  # the live session is untouched too
            await b.disconnect()

        asyncio.run(run())

    def test_close_tears_down_every_session_and_pending_finish(self):
        async def run():
            broker = _broker()
            a = _Client(broker, "m1", 5001)
            b = _Client(broker, "m2", 5002)
            await a.settle(broker)
            await b.settle(broker)
            await a.send(_project_file("alice.3mf"))
            pending = a.session(broker).finish_task
            self.assertIsNotNone(pending)
            await broker.close()
            await asyncio.gather(a.task, b.task, return_exceptions=True)
            self.assertEqual(broker._sessions, set())
            self.assertTrue(a.writer.closed and b.writer.closed)
            self.assertTrue(pending.cancelled() or pending.done())

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
