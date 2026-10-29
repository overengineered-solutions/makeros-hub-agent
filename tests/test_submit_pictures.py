"""Submit payloads from local sliced files; the site is a fake HTTP function."""
import base64
from datetime import datetime, timezone
import io
import zipfile

import pytest

from makeros_hub import agent
from makeros_hub.config import Config
from makeros_hub.http import Response
from makeros_hub.printers.threemf_objects import PLATE_PICTURE_MAX
from makeros_hub.vprinter.capture import CapturedJob, build_vp_submit_body
from test_ingest import build_multipart
from test_moonraker import door, threemf
from test_plate_facts import png


@pytest.mark.parametrize("route", ["moonraker", "octoprint", "virtual"])
@pytest.mark.parametrize("pictures,expected", [
    ({"ps_views_N.png": png(), "plate_N.png": png(2, 2)}, png()),
    ({"plate_N.png": png(2, 2), "plate_N_small.png": png()}, png(2, 2)),
    ({"plate_N_small.png": png()}, png()),
    ({"ps_views_N.png": b"x" * (PLATE_PICTURE_MAX + 1), "plate_N.png": png()}, png()),
    ({"plate_N.png": png() + b"x" * PLATE_PICTURE_MAX}, None),
    ({"plate_N.png": b"not a PNG"}, None),
    ({}, None),
], ids=["shop-views", "plate", "small", "oversized-fallback", "oversized", "invalid", "missing"])
def test_submit_plate_picture(door, monkeypatch, route, pictures, expected):
    plate = 1 if route == "octoprint" else 2
    buffer = io.BytesIO(threemf(plates=(plate,)))
    with zipfile.ZipFile(buffer, "a", zipfile.ZIP_STORED) as archive:
        for name, data in pictures.items():
            archive.writestr("Metadata/" + name.replace("N", str(plate)), data)
        archive.writestr("Metadata/plate_3.png", png(3, 3))  # Never borrow another plate's image.
    data = buffer.getvalue()
    sent = []
    monkeypatch.setattr(agent, "post_json", lambda url, body, **kw: sent.append(body) or
                        Response(200, {"ok": True, "jobId": "job", "submissionProtocol": "moonraker-v1"}))
    submit = agent.make_cloud_submit(Config(cloud_url="https://example.invalid"), "synthetic-hub")
    if route == "virtual":
        path = door.path / "part.3mf"
        path.write_bytes(data)
        job = CapturedJob(member_id="alice", filename=path.name, file_path=path, sha256="a" * 64,
                          size=len(data), ams_mapping=[], use_ams=False, required_filaments=[],
                          submitted_at=datetime.now(timezone.utc), plate=plate)
        body = build_vp_submit_body(job, model="P2S")
    else:
        if route == "moonraker":
            door.restart(pool="p2s")
        door.server._server.RequestHandlerClass.submit_fn = staticmethod(submit)
        if route == "moonraker":
            path = door.stage(name="part.3mf", data=data)
            assert door.start(path)[0] == 200
        else:
            multipart = build_multipart("PICTURE", "part.3mf", data, {"print": "true"})
            assert door.request("/api/files/local", method="POST", data=multipart,
                                content_type="multipart/form-data; boundary=PICTURE")[0] == 201
        assert len(sent) == 1
        body = sent[0]
    if expected is None:
        assert "plateImage" not in body
    else:
        assert base64.b64decode(body["plateImage"], validate=True) == expected
