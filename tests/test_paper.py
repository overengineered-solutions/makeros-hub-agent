"""v0.73 paper lane (2026-10-18): the IPP messages, and one job end to end with fakes for the site, the sandbox and the printer."""
import json
import os
import struct

from makeros_hub import paper

JID = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"


def test_print_job_request_carries_what_the_epson_needs():
    req = paper.print_job_request("192.168.1.42", "PS paper x", "tabloid", True, 3, True)
    assert req[:8] == struct.pack(">BBHI", 2, 0, 0x0002, 1)
    for want in (b"ipps://192.168.1.42/ipp/print", b"image/pwg-raster", b"na_ledger_11x17in", b"two-sided-long-edge", b"color"):
        assert want in req, want
    status, attrs = paper.parse_ipp(req[:8] + req[8:])          # our own encoder round-trips through our parser
    assert attrs["copies"] == [3] and attrs["print-quality"] == [4] and attrs["media"] == ["na_ledger_11x17in"]
    mono = paper.print_job_request("192.168.1.42", "j", "letter", False, 1, False)
    assert b"monochrome" in mono and b"one-sided" in mono and b"na_letter_8.5x11in" in mono


def test_parse_ipp_reads_a_job_id_and_status():
    resp = struct.pack(">BBHI", 2, 0, 0x0000, 1) + b"\x01" + paper._attr(0x47, "attributes-charset", "utf-8") + b"\x02" \
        + paper._attr(0x21, "job-id", 57) + paper._attr(0x23, "job-state", 3) + b"\x03"
    status, attrs = paper.parse_ipp(resp)
    assert status == 0 and attrs["job-id"] == [57] and attrs["job-state"] == [3]


def _run(tmp_path, result, answer, sender_fails=False, followed=(True, "printed"), report_fails=False):
    posted = []

    def getter(path, dest):
        assert path == f"/api/print/hub/paper-file/{JID}"
        open(dest, "wb").write(b"%PDF-1.4 fake")

    def runner(uid):
        work = tmp_path / ("paper-" + uid)
        job = json.load(open(work / "job.json"))
        assert job == {"file": "doc.pdf", "media": "letter", "colour": True}
        (work / "out").mkdir()
        (work / "out" / "result.json").write_text(json.dumps(result))
        if result.get("ok"):
            (work / "out" / "doc.pwg").write_bytes(b"RaS2")
        return 0

    def poster(body):
        posted.append(body)
        if report_fails and body.get("done") is True:
            raise OSError("site response lost after printing")
        return answer if "pages" in body else {"ok": True}

    def sender(host, pwg, name, media, colour, copies, two_sided):
        if sender_fails:
            raise OSError("refused")
        assert os.path.getsize(pwg) == 4 and media == "letter" and colour and copies == 2 and not two_sided
        return 57

    out = paper.process({"id": JID, "fileType": "pdf", "media": "letter", "colour": True, "copies": 2, "path": f"/api/print/hub/paper-file/{JID}"},
                        "192.168.1.42", str(tmp_path), getter=getter, poster=poster, runner=runner, sender=sender,
                        follower=lambda host, job, deadline: followed)
    assert not any(p.is_dir() and p.name.startswith("paper-") for p in tmp_path.iterdir()), "the work directory is removed"
    return out, posted


def test_an_approved_job_prints_and_reports(tmp_path):
    out, posted = _run(tmp_path, {"ok": True, "pages": [{"c": 0, "m": 0, "y": 0, "k": 0.01}]}, {"print": True})
    assert out == "printed"
    assert posted == [{"id": JID, "pages": [{"c": 0, "m": 0, "y": 0, "k": 0.01}]}, {"id": JID, "done": True, "detail": "printed"}]


def test_nothing_prints_unless_the_site_approves(tmp_path):
    out, posted = _run(tmp_path, {"ok": True, "pages": [{"c": 0.3, "m": 0.3, "y": 0.3, "k": 0}]}, {"print": False, "reason": "over the allowance"})
    assert out == "not approved by the site" and len(posted) == 1


def test_a_file_the_renderer_refuses_is_reported_not_printed(tmp_path):
    out, posted = _run(tmp_path, {"ok": False, "reason": "the PDF could not be read"}, {"print": True})
    assert out == "refused by the renderer" and posted == [{"id": JID, "measured": False, "reason": "the PDF could not be read"}]


def test_a_send_error_is_unknown_because_the_printer_may_have_received_it(tmp_path):
    out, posted = _run(tmp_path, {"ok": True, "pages": [{"c": 0, "m": 0, "y": 0, "k": 0.01}]}, {"print": True}, sender_fails=True)
    assert "unknown" in out and posted[-1]["uncertain"] is True


def test_bad_jobs_and_addresses_are_skipped(tmp_path):
    called = []
    kw = dict(getter=lambda *a: called.append(a), poster=lambda b: called.append(b))
    assert paper.process({"id": "../x", "fileType": "pdf", "media": "letter"}, "192.168.1.42", str(tmp_path), **kw).startswith("skipped")
    assert paper.process({"id": JID, "fileType": "exe", "media": "letter"}, "192.168.1.42", str(tmp_path), **kw).startswith("skipped")
    assert paper.process({"id": JID, "fileType": "pdf", "media": "letter"}, "8.8.8.8", str(tmp_path), **kw).startswith("skipped")
    assert called == []


def test_lost_success_report_is_retried_without_reporting_a_print_failure(tmp_path):
    out, posted = _run(tmp_path, {"ok": True, "pages": [{"c": 0, "m": 0, "y": 0, "k": 0.01}]},
                       {"print": True}, report_fails=True)
    assert not any(p.get("done") is False for p in posted)
    assert "pending" in out
    replayed = []
    assert paper.flush_reports(str(tmp_path), lambda b: replayed.append(b) or {"ok": True})
    assert replayed == [{"id": JID, "done": True, "detail": "printed"}]
    assert paper.flush_reports(str(tmp_path), lambda b: replayed.append(b) or {"ok": True})
    assert len(replayed) == 1


def test_a_timeout_does_not_claim_the_printer_failed(tmp_path):
    out, posted = _run(tmp_path, {"ok": True, "pages": [{"c": 0, "m": 0, "y": 0, "k": 0.01}]},
                       {"print": True}, followed=(None, "printer outcome unknown"))
    assert posted[-1] == {"id": JID, "uncertain": True, "detail": "printer outcome unknown"}


def test_follow_timeout_is_unknown_and_ipp_error_cannot_confirm_completion(monkeypatch):
    monkeypatch.setattr(paper, "_ipp", lambda *a, **k: (0x0400, {"job-state": [9]}))
    clock = iter([0, 2])
    ok, detail = paper.follow("192.168.1.42", 57, 1, sleep=lambda _: None, now=lambda: next(clock))
    assert ok is None and "not finished" in detail


def test_a_rejected_outcome_report_stays_durable(tmp_path):
    from makeros_hub import report_outbox
    report = {"id": JID, "done": True, "detail": "printed"}
    report_outbox.save([report], tmp_path / "paper-outcome.json")
    assert not paper.flush_reports(str(tmp_path), lambda _: {"error": "temporarily unavailable"})
    posted = []
    assert paper.flush_reports(str(tmp_path), lambda b: posted.append(b) or {"ok": True})
    assert posted == [report]


def test_no_dispatch_without_a_durable_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(paper.report_outbox, "save", lambda *a: False)
    out, posted = _run(tmp_path, {"ok": True, "pages": [{"c": 0, "m": 0, "y": 0, "k": 0.01}]}, {"print": True})
    assert out == "not sent: could not persist outcome guard"
    assert len(posted) == 1


def test_a_corrupt_receipt_blocks_new_dispatch(tmp_path):
    (tmp_path / "paper-outcome.json").write_text("broken")
    out, posted = _run(tmp_path, {"ok": True}, {"print": True})
    assert out == "outcome report pending" and posted == []
