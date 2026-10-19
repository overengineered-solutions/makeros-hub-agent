#!/usr/bin/env python3
"""ps_paper: one member paper print, inside the ps-paper container (paper/Dockerfile).

The hub writes /job/job.json {"file": "doc.pdf|doc.jpg|doc.png", "media": "letter|tabloid", "colour": bool} and the file,
then runs this through /opt/ps-paper/run-paper. Output in /job/out: doc.pwg (PWG raster at 300 dpi, sRGB or grey, fitted
to the page) and result.json {"ok": true, "pages": [{"c":..,"m":..,"y":..,"k":..}, ...]} with each page's ink coverage
(0 to 1 per channel, Ghostscript's inkcov), or {"ok": false, "reason": "..."}. The site counts colour and dense pages
from the coverage and decides against the member's allowance before the hub sends anything to the printer.
"""
import json, os, re, subprocess, sys

JOB = os.environ.get("PS_JOB_DIR", "/job")
MAX_PAGES = 30
PAPER = {"letter": "letter", "tabloid": "11x17"}
GS = ["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dNOINTERPOLATE"]


def fail(reason: str) -> int:
    os.makedirs(os.path.join(JOB, "out"), exist_ok=True)
    with open(os.path.join(JOB, "out", "result.json"), "w", encoding="utf-8") as fh:
        json.dump({"ok": False, "reason": reason[:300]}, fh)
    return 0


def kind_of(path: str) -> str | None:
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return None


def image_to_pdf(src: str, dst: str) -> None:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 60_000_000          # a decompression bomb is refused, not rendered
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.save(dst, "PDF", resolution=300.0)


def parse_inkcov(text: str) -> list[dict]:
    """Ghostscript's inkcov lines ("0.01234  0.00000  0.00000  0.04567 CMYK OK") -> per-page coverage. Pure."""
    pages = []
    for line in text.splitlines():
        m = re.match(r"\s*([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+CMYK OK", line)
        if m:
            c, mg, y, k = (min(1.0, max(0.0, float(v))) for v in m.groups())
            pages.append({"c": round(c, 5), "m": round(mg, 5), "y": round(y, 5), "k": round(k, 5)})
    return pages


def run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def main() -> int:
    try:
        job = json.load(open(os.path.join(JOB, "job.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return fail("no job.json")
    name, media, colour = job.get("file"), job.get("media"), job.get("colour") is True
    if not isinstance(name, str) or not re.fullmatch(r"doc\.(pdf|jpg|png)", name) or media not in PAPER:
        return fail("bad job.json")
    src = os.path.join(JOB, name)
    kind = kind_of(src)
    if kind is None:
        return fail("the file is not a PDF, JPEG or PNG")
    os.makedirs(os.path.join(JOB, "out"), exist_ok=True)
    pdf = src
    if kind != "pdf":
        pdf = "/tmp/doc.pdf"
        try:
            image_to_pdf(src, pdf)
        except Exception as e:  # noqa: BLE001 - a broken image is the member's file, not our fault
            return fail(f"the image could not be read ({type(e).__name__})")
    r = run(GS + ["-o", "-", "-sDEVICE=inkcov", pdf], 240)
    pages = parse_inkcov(r.stdout)
    if r.returncode != 0 or not pages:
        return fail("the PDF could not be read")
    if len(pages) > MAX_PAGES:
        return fail(f"{len(pages)} pages; the limit is {MAX_PAGES} a print")
    r = run(GS + ["-sDEVICE=pwgraster", "-r300", "-dcupsBitsPerColor=8", f"-dcupsColorSpace={19 if colour else 18}",
                  f"-sPAPERSIZE={PAPER[media]}", "-dFIXEDMEDIA", "-dPDFFitPage", "-o", os.path.join(JOB, "out", "doc.pwg"), pdf], 480)
    if r.returncode != 0 or not os.path.exists(os.path.join(JOB, "out", "doc.pwg")):
        return fail("the pages could not be rendered")
    with open(os.path.join(JOB, "out", "result.json"), "w", encoding="utf-8") as fh:
        json.dump({"ok": True, "pages": pages, "bytes": os.path.getsize(os.path.join(JOB, "out", "doc.pwg"))}, fh)
    return 0


def selftest() -> None:
    assert parse_inkcov(" 0.01234  0.00000  0.00000  0.04567 CMYK OK\n 0.20000  0.30000  0.10000  0.00000 CMYK OK\nnoise") == [
        {"c": 0.01234, "m": 0.0, "y": 0.0, "k": 0.04567}, {"c": 0.2, "m": 0.3, "y": 0.1, "k": 0.0}]
    assert parse_inkcov("") == []
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["selftest"]:
        selftest()
    else:
        sys.exit(main())
