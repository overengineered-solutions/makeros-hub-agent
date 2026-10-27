"""The converter's boundaries without LibreOffice; the image build tests the actual converters."""
import base64
import hashlib
import importlib.util
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from makeros_hub import paper

spec = importlib.util.spec_from_file_location("ps_paper", Path(__file__).parents[1] / "paper" / "ps_paper.py")
convert = importlib.util.module_from_spec(spec)
spec.loader.exec_module(convert)


def test_page_ranges_are_bounded_deduplicated_and_in_document_order():
    assert convert.page_range("all", 200) == list(range(200))
    assert convert.page_range("5,1-3,2", 5) == [0, 1, 2, 4]
    for value, count in [("0", 4), ("3-1", 4), ("5", 4), ("1,", 4), ("all", 201), ("all", 0), ("1" * 801, 200), (None, 1)]:
        with pytest.raises(ValueError):
            convert.page_range(value, count)


def test_fit_fill_and_actual_size_center_without_distorting():
    assert convert.placement(100, 50, (0, 0, 200, 200), "fit") == (0, 50, 200, 150)
    assert convert.placement(100, 50, (0, 0, 200, 200), "fill") == (-100, 0, 300, 200)
    assert convert.placement(100, 50, (0, 0, 200, 200), "actual") == (50, 75, 150, 125)
    assert convert.options_of(None, "pdf") == dict(pages="all", orientation="auto", fit="fit", slides=1, gridlines=False)
    for n in (1, 2, 3, 4, 6, 9):
        assert convert.options_of({"slides": n}, "pptx")["slides"] == n
    for kind, options in [("docx", {"fit": "fill"}), ("pdf", {"slides": 2}), ("jpeg", {"gridlines": True}), ("pptx", {"slides": True}), ("xls", {"gridlines": "yes"}), ("pdf", {"extra": True})]:
        with pytest.raises(ValueError):
            convert.options_of(options, kind)


def test_office_uses_a_private_profile_highest_macro_security_and_bounded_worker(tmp_path, monkeypatch):
    dest, profile = tmp_path / "converted.pdf", tmp_path / "profile"
    def run(args, timeout):
        assert args[1:3] == [convert.__file__, "office"] and timeout == 120
        assert "MacroSecurityLevel" in (profile / "user" / "registrymodifications.xcu").read_text()
        assert "<value>3</value>" in (profile / "user" / "registrymodifications.xcu").read_text()
        dest.write_bytes(b"%PDF-1.4")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(convert, "run", run)
    convert.office_convert("/job/doc.docx", str(dest), "docx", convert.options_of(None, "docx"), "letter", str(profile))
    command = convert.office_command(str(profile))
    assert command[0] == "libreoffice" and "--headless" in command
    assert "-env:UserInstallation=" + profile.as_uri() in command
    assert not any("socket" in a for a in command)


def test_output_limits_and_invalid_ink_fail(tmp_path):
    f = tmp_path / "out"
    f.write_bytes(b"a" * 11)
    with pytest.raises(ValueError):
        convert.bounded_file(f, 10)
    assert convert.bounded_file(f, 11) == 11
    with pytest.raises(ValueError):
        convert.parse_inkcov("2.0 0.0 0.0 0.0 CMYK OK")
    assert convert.MAX_PAGES == 200 and convert.MAX_RASTER == 512 * 1024 * 1024


def test_capability_comes_from_installed_renderer_and_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(paper, "_formats_cache", (0, []))
    monkeypatch.setattr(paper.subprocess, "run", lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0, stdout='["pdf","docx","heic"]'))
    assert paper.paper_formats() == ["docx", "heic", "pdf"]
    assert paper.paper_formats() == ["docx", "heic", "pdf"]
    assert len(calls) == 1 and calls[0][-1] == "formats"
    monkeypatch.setattr(paper, "_formats_cache", (0, []))
    monkeypatch.setattr(paper.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=2, stdout="old runner"))
    assert paper.paper_formats() == []


JID = "01234567-89ab-4cde-8f01-23456789abcd"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 32, 32)).decode()


def test_new_job_transports_options_and_preview_and_waits_for_approval(tmp_path):
    posted, configs = [], []
    options = dict(pages="1-3,5", slides=2, orientation="landscape", fit="fit", gridlines=False)
    job = dict(id=JID, fileType="pptx", media="letter", preview=True, options=options)
    def runner(uid):
        work = tmp_path / ("paper-" + uid)
        configs.append(json.loads((work / "job.json").read_text()))
        (work / "out").mkdir()
        (work / "out" / "doc.pwg").write_bytes(b"RaS2")
        (work / "out" / "result.json").write_text(json.dumps(dict(ok=True, pages=[dict(c=0, m=0, y=0, k=.01)] * 2,
            preview=[PNG] * 2, sourcePages=5, renderHash=hashlib.sha256(b"RaS2").hexdigest())))
        return 0
    out = paper.process(job, "192.168.1.42", str(tmp_path), formats=["pptx"], runner=runner,
        getter=lambda path, dest: Path(dest).write_bytes(b"PK doc"), poster=lambda b: posted.append(b) or {"print": False},
        sender=lambda *a: pytest.fail("a converted file must wait for site approval"))
    assert out == "not approved by the site"
    assert configs == [dict(file="doc.pptx", media="letter", colour=False, options=options, preview=True)]
    assert posted[0]["sourcePages"] == 5 and posted[0]["preview"] == [PNG] * 2
    assert len(posted[0]["pages"]) == 2


def test_missing_capability_refuses_before_fetching(tmp_path):
    posted = []
    outcome = paper.process(dict(id=JID, fileType="docx", media="letter", preview=True), "192.168.1.42", str(tmp_path), formats=[],
        getter=lambda *a: pytest.fail("must not fetch"), poster=lambda b: posted.append(b))
    assert outcome == "missing paper capability" and posted[0]["measured"] is False


def test_bad_renderer_reports_never_approve_a_print(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "doc.pwg").write_bytes(b"RaS2")
    for bad in [None, dict(ok=True, pages=[]), dict(ok=True, pages=[dict(c=float("nan"), m=0, y=0, k=0)])]:
        with pytest.raises(ValueError):
            paper.measurement(bad, str(tmp_path), True)
    valid = dict(ok=True, pages=[dict(c=0, m=0, y=0, k=.01)], sourcePages=1, preview=[PNG], renderHash="0" * 64)
    with pytest.raises(ValueError, match="changed"):
        paper.measurement(valid, str(tmp_path), True)
    valid["preview"] = ["a" * (24 * 1024 + 1)]
    with pytest.raises(ValueError, match="large"):
        paper.measurement(valid, str(tmp_path), True)


def test_an_older_site_cannot_send_an_office_file_without_preview_protocol(tmp_path):
    posted = []
    assert paper.process(dict(id=JID, fileType="docx", media="letter"), "192.168.1.42", str(tmp_path), formats=["docx"],
        getter=lambda *a: pytest.fail("must not fetch"), poster=lambda b: posted.append(b)) == "missing preview protocol"
    assert posted[0]["measured"] is False


def test_spreadsheet_width_actual_size_gridlines_and_orientation():
    defaults = convert.sheet_properties(convert.options_of(None, "xlsx"), "letter", False)
    assert defaults == {"PrintGrid": False, "PageScale": 0, "ScaleToPages": 0, "ScaleToPagesX": 1, "ScaleToPagesY": 0,
                        "IsLandscape": False, "Width": 21590, "Height": 27940}
    actual = convert.sheet_properties(convert.options_of(dict(fit="actual", gridlines=True, orientation="landscape"), "ods"), "tabloid", False)
    assert actual["PrintGrid"] is True and actual["PageScale"] == 100 and actual["ScaleToPagesX"] == 0
    assert actual["Width"] == 43180 and actual["Height"] == 27940


def test_handout_count_and_geometry_use_only_selected_slides(tmp_path, monkeypatch):
    class Rect:
        def __init__(self, coordinates):
            self.width = coordinates[2] - coordinates[0]
            self.height = coordinates[3] - coordinates[1]
    placed = []
    class Page:
        rect = Rect((0, 0, 720, 540))
        def get_contents(self):
            return [1]
        def show_pdf_page(self, rect, doc, index):
            placed.append(index)
    class Doc:
        def __init__(self, count):
            self.count = count
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def __len__(self):
            return self.count
        def __getitem__(self, index):
            return Page()
        def new_page(self, **kw):
            self.count += 1
            return Page()
        def save(self, *a, **kw):
            pass
    monkeypatch.setitem(__import__("sys").modules, "fitz", SimpleNamespace(Rect=Rect, open=lambda path=None: Doc(5 if path else 0)))
    for slides in (1, 2, 3, 4, 6, 9):
        placed.clear()
        source, printed = convert.layout("slides.pdf", str(tmp_path / "out.pdf"), "pptx", convert.options_of(dict(pages="1-3,5", slides=slides), "pptx"), "letter")
        assert source == 5 and printed == (4 + slides - 1) // slides and placed == [0, 1, 2, 4]
