"""Real HTTP door, fake control plane. No physical printer or production credentials."""
from concurrent.futures import ThreadPoolExecutor
import copy
import io
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import zipfile

import pytest

from makeros_hub import agent, moonraker
from makeros_hub.config import Config
from makeros_hub.http import Response, TransportError
from makeros_hub.ingest import IngestServer
from makeros_hub.printers import ingest_facts
from makeros_hub.printers import bambu as bambu_module, bambu_send
from makeros_hub.printers.manager import PrinterManager
from makeros_hub.vprinter.pool_ranking import PoolRankingState
from test_gcode_meta import sv08_gcode
from test_ingest import build_multipart
from test_tray_dispatch_order import FakeClient, RAW_STATE


def gcode(model="PS SV08 MAX", **kwargs):
    return sv08_gcode(model=model, **kwargs).replace(
        "; CONFIG_BLOCK_START", "; CONFIG_BLOCK_START\n; nozzle_diameter = 0.4\n; curr_bed_type = Textured PEI Plate").encode()


def threemf(plates=(1,), model="PS P2S", model_id="N7", **kwargs):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        xml = "<config>"
        for plate in plates:
            archive.writestr(f"Metadata/plate_{plate}.gcode", gcode(model, **kwargs))
            xml += (f'<plate><metadata key="index" value="{plate}"/>'
                    f'<metadata key="printer_model_id" value="{model_id}"/>'
                    '<metadata key="nozzle_diameter" value="0.4"/>'
                    '<filament id="1" type="PLA" color="#FFFFFF" used_g="4.53"/></plate>')
        archive.writestr("Metadata/slice_info.config", xml + "</config>")
    return buffer.getvalue()


class Door:
    def __init__(self, path):
        self.path = path
        self.calls, self.jobs = [], {}
        self.auth_calls = []
        self.revoked = set()
        self.available = True
        self.submit_mode = "ok"
        self.pools = moonraker.LivePools()
        self.ranking = PoolRankingState(path / "ranking.json")
        self.statuses = [
            {"printerId": "a", "model": "P2S", "connectionState": "connected", "ams": [{"trays": [
                {"slot": 0, "material": "PLA", "colorHex": "FFFFFFFF", "filamentId": "GFL99"}]}]},
            {"printerId": "b", "model": "P2S", "connectionState": "connected", "ams": [{"trays": [
                {"slot": 0, "material": "PETG", "colorHex": "FF0000FF", "filamentId": "GFG99"}]}]},
        ]
        self.refresh()
        self.open()

    def refresh(self):
        self.pools.update(self.statuses, self.ranking)

    def authorize(self, token, pool):
        self.auth_calls.append((token, pool))
        if not self.available:
            raise moonraker.DoorError(503, "Control plane unavailable")
        if token not in {"alice", "bob"} or token in self.revoked:
            return {"ok": False}
        pid = "a" if token == "alice" else "b"
        return {"ok": True, "submissionProtocol": moonraker.PROTOCOL, "memberId": token,
                "modelPool": pool, "printerModel": ingest_facts.POOLS[pool][0], "printerIds": [pid],
                "loadedSpools": {pid: {"material": "PLA", "colorHex": "#FFFFFF"}}}

    def submit(self, **kwargs):
        self.calls.append(kwargs)
        uid = kwargs["submission_uid"]
        if self.submit_mode == "before_commit":
            return {"status": "error"}
        if kwargs["member_token"] in self.revoked:
            return {"status": "bad_token"}
        self.jobs.setdefault(uid, {"status": "queued", "jobId": "job-" + uid})
        if self.submit_mode == "lost_reply":
            raise TimeoutError("reply lost after cloud committed")
        return self.jobs[uid]

    def open(self, *, pool="sv08-max"):
        self.pool = pool
        self.server = IngestServer(self.submit, port=0, spool_dir=self.path, max_bytes=2 * 1024**2,
                                   moonraker_authorize=self.authorize, moonraker_pools=self.pools,
                                   moonraker_pool=pool)
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server._server.server_address[1]}"
        self.store = self.server._server.RequestHandlerClass.moonraker.store

    def restart(self, *, pool=None):
        self.server.stop()
        self.open(pool=pool or self.pool)

    def request(self, path, *, method="GET", token="alice", data=None, content_type=None):
        headers = {"X-Api-Key": token}
        if content_type:
            headers["Content-Type"] = content_type
        if isinstance(data, dict):
            headers["Content-Type"] = "application/json"
            data = json.dumps(data).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            response = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            assert response.headers.get("Cache-Control") == "no-store"
            return response.status, json.loads(response.read() or b"{}")

    def upload(self, *, name="part.gcode", data=None, fields=None, **kwargs):
        data = gcode() if data is None else data
        body = build_multipart("BOUNDARY", name, data, {"root": "gcodes", **(fields or {})})
        return self.request("/server/files/upload", method="POST", data=body,
                            content_type="multipart/form-data; boundary=BOUNDARY", **kwargs)

    def stage(self, **kwargs):
        status, body = self.upload(**kwargs)
        assert status == 201, body
        assert body["result"]["print_started"] is False
        return body["result"]["item"]["path"]

    def start(self, path, **kwargs):
        return self.request("/printer/print/start", method="POST", data={"filename": path}, **kwargs)


@pytest.fixture
def door(tmp_path):
    d = Door(tmp_path)
    yield d
    d.server.stop()


def test_connection_info_identifies_pool_and_cloud_availability(door):
    door.restart(pool="p2s")
    status, body = door.request("/server/info")
    assert status == 200
    assert body["result"]["klippy_state"] == "ready"
    assert body["result"]["model_pool"] == "p2s"
    assert "P2S" in body["result"]["machine_name"]
    assert door.request("/server/files/roots")[1]["result"] == [{"name": "gcodes", "permissions": "rw"}]
    assert door.request("/moonraker/p2s/server/info")[0] == 404
    assert door.request("/moonraker/typo/server/info")[0] == 404
    assert door.request("/server/info", token="bad")[0] == 403
    assert door.request("/server/info", token="")[0] == 403
    door.available = False
    assert door.request("/server/info")[0] == 503
    assert door.calls == []


def test_upload_only_even_with_print_true_creates_zero_jobs(door):
    path = door.stage(fields={"print": "true"})
    assert (door.path / path).read_bytes() == gcode()
    assert door.calls == [] and door.jobs == {}
    with door.store.locked() as db:
        row = db.execute("SELECT * FROM uploads").fetchone()
        assert row["state"] == "staged"
        assert row["member"] == "alice"
        assert "alice" not in path


def test_two_members_same_filename_are_separate_and_bound_to_pool(door):
    door.restart(pool="p2s")
    a = door.stage(name="part.3mf", data=threemf())
    b = door.stage(token="bob", name="part.3mf", data=threemf())
    assert a != b
    assert (door.path / a).exists() and (door.path / b).exists()
    assert door.start(a, token="bob")[0] == 404
    assert door.start(b, token="alice")[0] == 404
    door.restart(pool="sv08-max")
    assert door.start(a)[0] == 404
    door.restart(pool="p2s")
    assert door.calls == []
    assert door.start(a)[0] == 200
    assert door.start(b, token="bob")[0] == 200
    assert len(door.jobs) == 2


def test_repeat_and_concurrent_start_submit_once_and_return_same_receipt(door):
    path = door.stage()
    with ThreadPoolExecutor(max_workers=3) as workers:
        results = list(workers.map(lambda _: door.start(path), range(3)))
    assert all(r == results[0] for r in results)
    assert results[0][0] == 200
    assert door.start(path) == results[0]
    assert len(door.calls) == len(door.jobs) == 1


def test_restart_before_and_after_start_preserves_uid_and_receipt(door):
    path = door.stage()
    door.restart()
    receipt = door.start(path)
    assert receipt[0] == 200
    door.restart()
    assert door.start(path) == receipt
    assert len(door.calls) == len(door.jobs) == 1
    assert door.calls[0]["submission_uid"] == path.split("/")[0]


@pytest.mark.parametrize("mode", ["before_commit", "lost_reply"])
def test_unknown_outcome_then_restart_retries_same_uid_one_cloud_job(door, mode):
    path = door.stage()
    door.submit_mode = mode
    assert door.start(path)[0] == 502
    door.restart()
    assert (door.path / path).exists()
    door.submit_mode = "ok"
    receipt = door.start(path)
    assert receipt[0] == 200
    assert len(door.jobs) == 1
    assert len(door.calls) == 2
    assert len({c["submission_uid"] for c in door.calls}) == 1
    assert door.start(path) == receipt


def test_crash_after_cloud_commit_before_local_receipt(door):
    path = door.stage()
    with door.store.locked() as db:
        db.execute("CREATE TRIGGER no_receipt BEFORE UPDATE OF job ON uploads BEGIN SELECT RAISE(FAIL,'simulated disk failure'); END")
    assert door.start(path)[0] == 507
    assert len(door.jobs) == 1
    with door.store.locked() as db:
        db.execute("DROP TRIGGER no_receipt")
    door.restart()
    assert door.start(path)[0] == 200
    assert len(door.jobs) == 1
    assert len({c["submission_uid"] for c in door.calls}) == 1


def test_independent_servers_share_lock_and_durable_receipt(door):
    path = door.stage()
    other = moonraker.StagedUploads(door.path)
    access = door.authorize("alice", "sv08-max")
    with ThreadPoolExecutor(max_workers=2) as workers:
        a = workers.submit(door.start, path)
        b = workers.submit(other.start, path, access, "alice", door.submit)
        assert a.result() == (200, b.result())
    assert len(door.calls) == len(door.jobs) == 1


def test_revocation_applies_to_reads_upload_start_and_receipt_replay(door):
    staged = door.stage()
    committed = door.stage()
    assert door.start(committed)[0] == 200
    door.revoked.add("alice")
    for path in ("/server/info", "/server/database/item?namespace=lane_data", "/server/files/roots"):
        assert door.request(path)[0] == 403
    assert door.upload()[0] == 403
    assert door.start(staged)[0] == 403
    assert door.start(committed)[0] == 403
    assert len(door.jobs) == 1


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_command_and_method_bypasses_are_refused(door, method):
    for path in ("/printer/gcode/script", "/printer/print/pause", "/printer/print/resume", "/printer/print/cancel",
                 "/machine/reboot", "/server/files/delete", "/printer/emergency_stop", "/printer/objects/query?mmu"):
        status, body = door.request(path, method=method, data={"script": "G28"} if method != "GET" else None)
        assert status == 405
        assert "PS site" in body["error"]["message"]
    if method != "POST":
        assert door.request("/printer/print/start", method=method)[0] == 405
    assert door.calls == []


@pytest.mark.parametrize("name", ["part.gcode", "../part.gcode", "/etc/passwd", "a" * 32 + "/../part.gcode",
                                  "a" * 32 + "/%2e%2e/part.gcode", "a" * 32 + "\\part.gcode"])
def test_forged_start_paths_do_not_resolve(door, name):
    assert door.start(name)[0] == 404
    assert door.calls == []


def test_lane_data_shape_membership_empty_remove_reorder_and_unknown_temps(door):
    door.restart(pool="p2s")
    endpoint = "/server/database/item?namespace=lane_data"
    status, body = door.request(endpoint)
    assert status == 200
    value = body["result"]["value"]
    assert body["result"]["namespace"] == "lane_data"
    assert value["lane1"] == {"lane": "0", "material": "PLA", "color": "#FFFFFF", "bed_temp": 0, "nozzle_temp": 0}
    assert len(value) == moonraker.LANES
    other = door.request(endpoint, token="bob")[1]["result"]["value"]
    assert other["lane1"]["material"] == "PETG"
    assert all(l["material"] != "PETG" for l in value.values())
    assert all(type(l["lane"]) is str and type(l["bed_temp"]) is int and type(l["nozzle_temp"]) is int for l in value.values())
    door.statuses[0]["ams"][0]["trays"] = [{"slot": 3, "material": "MYSTERY", "colorHex": "123456FF"}]
    door.refresh()
    assert door.request(endpoint)[1]["result"]["value"]["lane1"]["material"] == "MYSTERY"
    door.statuses = []
    door.refresh()
    empty = door.request(endpoint)[1]["result"]["value"]
    # {} makes Orca 2.4.2 return false; explicit empty lanes make it clear old trays.
    assert len(empty) == moonraker.LANES and all(l["material"] == "" for l in empty.values())


def test_failed_or_stale_refresh_never_serves_old_pool(door):
    door.restart(pool="p2s")
    endpoint = "/server/database/item?namespace=lane_data"
    assert door.request(endpoint)[0] == 200
    with pytest.raises(ValueError):
        door.pools.update(None, None)
    assert door.request(endpoint)[0] == 503
    door.refresh()
    door.pools.max_age = -1
    assert door.request(endpoint)[0] == 503
    door.pools.max_age = 90
    door.statuses[0]["connectionState"] = "error"
    door.refresh()
    assert door.request(endpoint)[0] == 503
    door.statuses[0]["connectionState"] = "offline"
    door.refresh()
    assert all(not lane["material"] for lane in door.request(endpoint)[1]["result"]["value"].values())
    door.available = False
    assert door.request(endpoint)[0] == 503


def test_klipper_uses_fresh_site_staff_spool_and_live_connected_state(door):
    status, body = door.request("/server/database/item?namespace=lane_data")
    assert status == 200 and body["result"]["value"]["lane1"]["material"] == "PLA"
    bad = door.authorize("alice", "sv08-max")
    bad["loadedSpools"] = {}
    with pytest.raises(moonraker.DoorError):
        moonraker.validate_access(bad, "sv08-max")
    bad["loadedSpools"] = {"a": None}
    assert all(not l["material"] for l in door.pools.lanes(moonraker.validate_access(bad, "sv08-max")).values())


def test_selected_plate_facts_survive_staging_restart_and_submit(door):
    door.restart(pool="p2s")
    original = threemf((1, 3))
    path = door.stage(name="part.3mf", data=original, fields={"plateindex": "3"})
    door.restart()
    assert door.start(path)[0] == 200
    call = door.calls[0]
    assert call["print_now"] is True
    assert call["printer_model"] == "P2S"
    assert call["est_grams"] == 5
    facts = call["file_facts"]
    assert facts["plate"] == 3 and facts["printerModelId"] == "N7"
    assert facts["bedType"] == "Textured PEI Plate"
    assert facts["requiredFilaments"] == [{"slot": 1, "type": "PLA", "color": "FFFFFFFF", "usedG": 4.53, "nozzleDiameterMm": 0.4}]
    assert (door.path / path).read_bytes() == original


@pytest.mark.parametrize("spool_moves", [False, True])
def test_staged_plate_reaches_existing_dispatch_and_live_spool_recheck(door, monkeypatch, spool_moves):
    door.restart(pool="p2s")
    path = door.stage(name="part.3mf", data=threemf((1, 3)), fields={"plateindex": "3"})
    receipt = door.start(path)[1]["result"]
    call = door.calls[-1]
    monkeypatch.setattr(bambu_module, "QUEUE_PROGRESS_DIR", door.path / "progress")
    monkeypatch.setattr(bambu_module, "TERMINAL_JOBS_DIR", door.path / "terminal")
    adapter = bambu_module.BambuAdapter("p", "127.0.0.1", "fake-serial", "fake-code", model="P2S")
    adapter._client = FakeClient()
    adapter._connack, adapter._state_seen = "ok", True
    adapter._data = copy.deepcopy(RAW_STATE)
    adapter._last_report_at = time.monotonic()
    uploads = []

    def upload(*args):
        uploads.append(args)
        if spool_moves:
            adapter._data["print"]["ams"]["ams"][0]["tray"] = []

    monkeypatch.setattr(bambu_send, "upload_3mf", upload)
    manager = PrinterManager()
    manager._adapters["p"] = adapter
    reports = manager.dispatch_assignments([{
        "queueJobId": receipt["jobId"], "printerId": "p", "submissionUid": call["submission_uid"],
        "fileName": call["file_name"], "plate": call["file_facts"]["plate"], "assignmentSeq": 1,
        "requiredFilaments": call["required_filaments"], "useAms": True,
    }], door.path)
    assert len(uploads) == 1
    assert Path(uploads[0][2]) == door.path / path
    if spool_moves:
        assert reports[0]["state"] == "held" and reports[0]["reason"].startswith("spool_mismatch")
        assert adapter._client.published == []
    else:
        assert reports[0]["state"] == "uploading"
        command = json.loads(adapter._client.published[0])["print"]
        assert command["param"] == "Metadata/plate_3.gcode"
        assert command["ams_mapping"] == [1]  # live white PLA is physical slot 1, not virtual lane 0


@pytest.mark.parametrize("fields", [{"plateindex": "0"}, {"plateindex": "2"}, {"plateindex": "x"}, {"plateindex": "65"}, {}])
def test_missing_invalid_or_unchosen_plate_is_refused(door, fields):
    door.restart(pool="p2s")
    assert door.upload(name="part.3mf", data=threemf((1, 3)), fields=fields)[0] == 400
    assert door.calls == []
    assert not list(door.path.glob("*/*.3mf"))


@pytest.mark.parametrize("kwargs", [
    {"pool": "giga"}, {"data": gcode(model="Unknown")},
    {"data": gcode(mm="1, 1", g="1, 1", types="PLA;PETG", colours="#FFFFFF;#000000")},
    {"data": gcode().replace(b"nozzle_diameter = 0.4", b"nozzle_diameter = ")},
    {"fields": {"root": "config"}}, {"fields": {"path": "../"}}, {"fields": {"unknown": "1"}},
    {"pool": "p2s", "name": "part.3mf", "data": threemf(model_id="O1D")},
    {"pool": "p2s", "name": "part.3mf", "data": threemf(model_id="")},
    {"pool": "p2s", "name": "part.3mf", "data": b"not a zip"},
    {"name": "part.3mf", "data": threemf()}, {"name": "part.stl", "data": b"solid"},
])
def test_file_facts_and_target_validation(door, kwargs):
    kwargs = kwargs.copy()
    door.restart(pool=kwargs.pop("pool", "sv08-max"))
    assert door.upload(**kwargs)[0] == 400
    assert door.calls == []


def test_missing_weight_stays_unknown(door):
    path = door.stage(data=gcode(g=None))
    assert door.start(path)[0] == 200
    assert door.calls[0]["file_facts"]["estGrams"] is None


def test_recheck_bytes_before_start(door):
    path = door.stage()
    (door.path / path).write_bytes(gcode(model="PS Giga"))
    assert door.start(path)[0] == 409
    assert door.calls == []


def test_sanitized_filename_can_always_be_started(door):
    path = door.stage(name="-part.gcode")
    assert door.start(path)[0] == 200
    assert len(door.calls) == 1


def test_moonraker_shares_legacy_upload_concurrency_limit(door):
    slot = door.server._server.RequestHandlerClass.upload_slot
    with slot:
        assert door.upload()[0] == 503
    assert door.upload()[0] == 201


@pytest.mark.parametrize("change", ["dual_nozzles", "duplicate_filament", "second_extruder"])
def test_ambiguous_3mf_filament_facts_are_refused(door, change):
    door.restart(pool="p2s")
    original = io.BytesIO(threemf())
    edited = io.BytesIO()
    with zipfile.ZipFile(original) as src, zipfile.ZipFile(edited, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if change == "dual_nozzles" and name.endswith(".gcode"):
                data = data.replace(b"nozzle_diameter = 0.4", b"nozzle_diameter = 0.4,0.6")
            elif change == "duplicate_filament" and name.endswith(".config"):
                data = data.replace(b"<filament", b'<filament id="1" type="PETG" color="#000000"/><filament')
            elif change == "second_extruder" and name.endswith(".config"):
                data = data.replace(b"<filament", b'<filament group_id="1"')
            dst.writestr(name, data)
    assert door.upload(name="part.3mf", data=edited.getvalue())[0] == 400
    assert door.calls == []


def test_expiry_and_crash_recovery_preserve_committed_and_uncertain_files(door):
    expires = door.stage()
    committed = door.stage()
    uncertain = door.stage()
    assert door.start(committed)[0] == 200
    door.submit_mode = "lost_reply"
    assert door.start(uncertain)[0] == 502
    partial_uid = "e" * 32
    partial = door.path / partial_uid
    partial.mkdir()
    (partial / ".upload.tmp").write_bytes(b"partial")
    with door.store.locked() as db:
        db.execute("INSERT INTO uploads(uid,member,pool,size,created,state) VALUES(?,?,?,?,?,'receiving')",
                   (partial_uid, "alice", "sv08-max", 100, time.time()))
        db.execute("UPDATE uploads SET created=0")
    door.restart()
    assert not partial.exists() and not (door.path / expires).exists()
    assert (door.path / committed).exists() and (door.path / uncertain).exists()
    with door.store.locked() as db:
        assert {r[0] for r in db.execute("SELECT state FROM uploads")} == {"queued", "submitting"}


def test_bounds_disk_count_bytes_member_and_busy(door, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(moonraker, "MAX_RECORDS", 0)
        assert door.upload()[0] == 507
    with monkeypatch.context() as m:
        m.setattr(moonraker, "MAX_SPOOL_BYTES", 1)
        assert door.upload()[0] == 507
    with monkeypatch.context() as m:
        m.setattr(moonraker, "MAX_MEMBER_STAGES", 0)
        assert door.upload()[0] == 507
    with monkeypatch.context() as m:
        m.setattr(moonraker.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 0})())
        assert door.upload()[0] == 507
    with door.store.locked():
        assert door.upload()[0] == 503
    handler = door.server._server.RequestHandlerClass
    monkeypatch.setattr(handler, "max_bytes", 10)
    assert door.upload()[0] == 413
    assert door.calls == []


def test_zip_bounds_duplicates_traversal_and_expansion(door):
    door.restart(pool="p2s")
    for entries in ([('Metadata/plate_1.gcode', b'x')] * 2,
                    [('../evil', b'x')], [('bomb', b'0' * 100_000)]):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in entries:
                archive.writestr(name, value)
        assert door.upload(name="part.3mf", data=data.getvalue())[0] == 400
    assert door.calls == []


@pytest.mark.parametrize("limit", ["MAX_ZIP_ENTRIES", "MAX_EXPANDED_BYTES", "MAX_SLICE_INFO_BYTES"])
def test_zip_count_expanded_and_metadata_limits(door, monkeypatch, limit):
    door.restart(pool="p2s")
    monkeypatch.setattr(ingest_facts, limit, 1)
    assert door.upload(name="part.3mf", data=threemf())[0] == 400
    assert door.calls == []


@pytest.mark.parametrize("limit", ["MAX_ZIP_ENTRIES", "MAX_ZIP_DIRECTORY_BYTES"])
def test_zip_directory_limit_is_checked_before_zipfile_allocates_entries(tmp_path, monkeypatch, limit):
    path = tmp_path / "part.3mf"
    path.write_bytes(threemf())
    monkeypatch.setattr(ingest_facts, limit, 1)

    def must_not_open(*args, **kwargs):
        pytest.fail("ZipFile allocates the full central directory before infolist() can bound it")

    monkeypatch.setattr(ingest_facts.zipfile, "ZipFile", must_not_open)
    with pytest.raises(ValueError):
        ingest_facts.read_file_facts(path, "p2s")


def test_zip64_directory_cannot_override_the_bounded_end_record(door):
    door.restart(pool="p2s")
    data = threemf()
    end = data.rfind(b"PK\x05\x06")
    data = data[:end] + b"PK\x06\x07" + bytes(16) + data[end:]
    assert door.upload(name="part.3mf", data=data)[0] == 400


def test_second_identical_upload_is_an_intentional_second_job(door):
    first, second = door.stage(), door.stage()
    assert first != second
    assert door.start(first)[0] == door.start(second)[0] == 200
    assert len(door.jobs) == 2


def test_cloud_session_wire_and_submit_contract(monkeypatch):
    calls = []
    access = {"ok": True, "submissionProtocol": moonraker.PROTOCOL, "memberId": "member",
              "modelPool": "p2s", "printerModel": "P2S", "printerIds": [], "loadedSpools": {}}
    def post(url, body, **kwargs):
        calls.append((url, body, kwargs))
        return Response(200, access if url.endswith("/session") else {
            "ok": True, "jobId": "job", "submissionProtocol": moonraker.PROTOCOL})
    monkeypatch.setattr(agent, "post_json", post)
    cfg = Config(cloud_url="https://ps.example")
    auth = agent.make_moonraker_authorize(cfg, "fake-hub")
    assert moonraker.validate_access(auth("fake-member", "p2s"), "p2s") == access
    assert calls[0] == ("https://ps.example/api/print/hub/moonraker/session",
                        {"memberToken": "fake-member", "modelPool": "p2s"},
                        {"bearer": "fake-hub", "timeout": 8, "retries": 0})
    facts = {"version": 1, "modelPool": "p2s", "plate": 3}
    result = agent.make_cloud_submit(cfg, "fake-hub")(
        member_token="fake-member", submission_uid="a" * 32, file_name="part.3mf", file_sha256="b" * 64,
        file_size=100, print_now=True, file_facts=facts)
    assert result == {"status": "queued", "jobId": "job"}
    sent = calls[-1][1]
    assert sent["printNow"] is True and sent["plate"] == 3
    assert sent["fileFacts"] == facts and sent["submissionProtocol"] == moonraker.PROTOCOL
    assert calls[-1][2]["retries"] == 0


@pytest.mark.parametrize("response", [Response(404, {}), Response(401, {}), Response(500, {}), Response(200, {})])
def test_old_or_unavailable_cloud_is_not_ready(monkeypatch, response):
    monkeypatch.setattr(agent, "post_json", lambda *a, **kw: response)
    auth = agent.make_moonraker_authorize(Config(cloud_url="https://ps.example"), "fake")
    with pytest.raises(moonraker.DoorError):
        moonraker.validate_access(auth("fake", "p2s"), "p2s")


def test_unversioned_submit_receipt_is_uncertain_and_auth_outage_is_unavailable(monkeypatch):
    cfg = Config(cloud_url="https://ps.example")
    monkeypatch.setattr(agent, "post_json", lambda *a, **kw: Response(200, {"ok": True, "jobId": "old"}))
    result = agent.make_cloud_submit(cfg, "fake")(
        member_token="alice", submission_uid="a" * 32, file_name="part.3mf", file_sha256="b" * 64,
        file_size=1, print_now=True, file_facts={"plate": 1, "modelPool": "p2s"})
    assert result["status"] == "error"

    def offline(*args, **kwargs):
        raise TransportError("offline")

    monkeypatch.setattr(agent, "post_json", offline)
    with pytest.raises(moonraker.DoorError) as error:
        agent.make_moonraker_authorize(cfg, "fake")("alice", "p2s")
    assert error.value.code == 503


@pytest.mark.parametrize("bad", [None, [], {"ok": True}, {"ok": False}])
def test_malformed_session_never_authorizes(bad):
    with pytest.raises(moonraker.DoorError):
        moonraker.validate_access(bad, "p2s")


def test_site_refusal_words_reach_the_slicer(door, monkeypatch):
    # The site writes plain reasons (plate, PLA-only, nozzle, staff-only test); its short codes keep the hub's own sentence.
    path = door.stage()
    handler = door.server._server.RequestHandlerClass
    words = "This test connection supports Textured PEI only. Use the site's sliced upload for another build plate."
    handler.submit_fn = staticmethod(lambda **_: {"status": "rejected", "reason": words})
    status, body = door.start(path)
    assert status == 403 and body["error"]["message"].startswith(words)
    handler.submit_fn = staticmethod(lambda **_: {"status": "rejected", "reason": "submission_uid_conflict"})
    assert door.start(path)[1]["error"]["message"].startswith("The site refused this start.")
    staff = "This Orca connection is being tested by staff. Use your existing PS connection."
    for reason, expected in ((staff, staff), ("invalid_token", "Print token or membership is no longer eligible.")):
        monkeypatch.setattr(agent, "post_json", lambda *a, _r=reason, **kw: Response(403, {"ok": False, "reason": _r}))
        with pytest.raises(moonraker.DoorError) as error:
            agent.make_moonraker_authorize(Config(cloud_url="https://ps.example"), "fake")("member", "p2s")
        assert error.value.code == 403 and str(error.value).startswith(expected)


def test_door_submit_sends_unknown_weight_as_explicit_null(monkeypatch):
    # The site's door schema requires estGrams (nullable, not optional); an omitted key refuses every unweighed start.
    sent = []
    monkeypatch.setattr(agent, "post_json", lambda url, body, **kw: sent.append(body) or Response(200, {}))
    agent.make_cloud_submit(Config(cloud_url="https://ps.example"), "fake")(
        member_token="alice", submission_uid="a" * 32, file_name="part.gcode", file_sha256="b" * 64, file_size=1,
        print_now=True, est_grams=None, file_facts={"plate": 1, "modelPool": "sv08-max", "estGrams": None})
    assert "estGrams" in sent[0] and sent[0]["estGrams"] is None
