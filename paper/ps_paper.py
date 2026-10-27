#!/usr/bin/env python3
"""Convert, lay out, measure and render one untrusted paper print in the offline container."""
import base64, hashlib, json, math, os, re, resource, shutil, subprocess, sys, tempfile, time, zipfile
from pathlib import Path

JOB = os.environ.get("PS_JOB_DIR", "/job")
MAX_PAGES = 200
MAX_FILE = 20 * 1024 * 1024
MAX_PDF = 100 * 1024 * 1024
MAX_RASTER = 512 * 1024 * 1024
MAX_PREVIEW = 512 * 1024
OFFICE = "docx doc pptx ppt xlsx xls odt odp ods rtf txt".split()
SLIDES = "pptx ppt odp".split()
SHEETS = "xlsx xls ods".split()
FORMATS = ["pdf", "jpeg", "png", *OFFICE, "heic", "heif"]
PAPER = {"letter": (612, 792), "tabloid": (792, 1224)}
GS = ["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dNOINTERPOLATE"]


def fail(reason):
    out = Path(JOB) / "out"
    out.mkdir(exist_ok=True)
    (out / "result.json").write_text(json.dumps({"ok": False, "reason": str(reason)[:300]}))
    return 0


def page_range(value, count):
    if not 1 <= count <= MAX_PAGES:
        raise ValueError("1 to 200 source pages per print")
    if value in ("", "all"):
        return list(range(count))
    if not isinstance(value, str) or len(value) > 800 or not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", value):
        raise ValueError("Pages: use all or a range like 1-3,5")
    pages = set()
    for part in value.split(","):
        ends = [int(n) for n in part.split("-")]
        a, b = ends[0], ends[-1]
        if not 1 <= a <= b <= count:
            raise ValueError(f"Pages must be between 1 and {count}")
        pages.update(range(a - 1, b))
    return sorted(pages)


def options_of(raw, kind):
    defaults = {"pages": "all", "orientation": "auto", "fit": "fit", "slides": 1, "gridlines": False}
    if raw is None:
        return defaults
    if not isinstance(raw, dict) or set(raw) - set(defaults):
        raise ValueError("Unknown paper options")
    o = {**defaults, **raw}
    page_range(o["pages"], MAX_PAGES)
    if o["orientation"] not in ("auto", "portrait", "landscape") or o["fit"] not in ("fit", "actual", "fill"):
        raise ValueError("Invalid orientation or fit")
    if type(o["slides"]) is not int or o["slides"] not in (1, 2, 3, 4, 6, 9) or type(o["gridlines"]) is not bool:
        raise ValueError("Invalid slides or gridlines")
    if (kind not in SLIDES and o["slides"] != 1) or (kind not in SHEETS and o["gridlines"]):
        raise ValueError("These options do not apply to that file")
    if o["fit"] == "fill" and kind not in ("jpeg", "png", "heic", "heif"):
        raise ValueError("Fill is only for pictures")
    if kind in SLIDES and o["fit"] != "fit":
        raise ValueError("Slides are fitted to their handout")
    return o


def office_command(profile):
    return ["libreoffice", "--headless", "--norestore", "--nodefault", "--nofirststartwizard",
            f"-env:UserInstallation={Path(profile).as_uri()}", "--accept=pipe,name=pspaper;urp;StarOffice.ComponentContext"]


def office_convert(src, dst, kind, options, media, profile):
    # Both the profile and the load call deny macros; no trusted locations survive this job.
    user = Path(profile) / "user"
    user.mkdir(parents=True)
    (user / "registrymodifications.xcu").write_text('''<?xml version="1.0"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting">
<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
</item></oor:items>''')
    r = run([sys.executable, __file__, "office", src, dst, kind, json.dumps(options), media, profile], 120)
    if r.returncode or not Path(dst).exists():
        raise ValueError("Office conversion failed. Try a PDF exported from the original app.")
    bounded_file(dst, MAX_PDF)


def sheet_properties(options, media, was_landscape):
    landscape = was_landscape if options["orientation"] == "auto" else options["orientation"] == "landscape"
    w, h = PAPER[media]
    return {"PrintGrid": options["gridlines"], "PageScale": 0 if options["fit"] == "fit" else 100,
            "ScaleToPages": 0, "ScaleToPagesX": 1 if options["fit"] == "fit" else 0, "ScaleToPagesY": 0,
            "IsLandscape": landscape, "Width": round((h if landscape else w) * 2540 / 72),
            "Height": round((w if landscape else h) * 2540 / 72)}


def office_worker(src, dst, kind, encoded, media, profile):
    import uno
    from com.sun.star.beans import PropertyValue
    from com.sun.star.document.MacroExecMode import NEVER_EXECUTE
    from com.sun.star.document.UpdateDocMode import NO_UPDATE
    def props(**values):
        return tuple(PropertyValue(Name=k, Value=v) for k, v in values.items())
    options = json.loads(encoded)
    proc = subprocess.Popen(office_command(profile), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    doc = None
    try:
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
        for attempt in range(100):
            try:
                context = resolver.resolve("uno:pipe,name=pspaper;urp;StarOffice.ComponentContext")
                break
            except Exception:
                if proc.poll() is not None or attempt == 99:
                    raise
                time.sleep(.1)
        desktop = context.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", context)
        doc = desktop.loadComponentFromURL(Path(src).as_uri(), "_blank", 0,
            props(Hidden=True, ReadOnly=True, MacroExecutionMode=NEVER_EXECUTE, UpdateDocMode=NO_UPDATE))
        if doc is None:
            raise ValueError("Office could not open the file")
        if kind in SHEETS:
            styles = doc.getStyleFamilies().getByName("PageStyles")
            for name in styles.getElementNames():
                style = styles.getByName(name)
                for key, value in sheet_properties(options, media, style.IsLandscape).items():
                    style.setPropertyValue(key, value)
        filter_name = "calc_pdf_Export" if kind in SHEETS else "impress_pdf_Export" if kind in SLIDES else "writer_pdf_Export"
        doc.storeToURL(Path(dst).as_uri(), props(FilterName=filter_name, Overwrite=True))
    finally:
        if doc is not None:
            doc.close(True)
        proc.terminate()
        proc.wait(timeout=10)


def bounded_file(path, limit):
    size = os.path.getsize(path)
    if not 0 < size <= limit:
        raise ValueError(f"Output exceeds its {limit // (1024 * 1024)} MB limit")
    return size


def image_to_pdf(src, dst):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 40_000_000
    with Image.open(src) as image:
        if image.width * image.height > Image.MAX_IMAGE_PIXELS:
            raise ValueError("Picture exceeds 40 million pixels")
        dpi = image.info.get("dpi", (300, 300))[0]
        dpi = dpi if isinstance(dpi, (int, float)) and 36 <= dpi <= 1200 else 300
        image = ImageOps.exif_transpose(image).convert("RGBA")
        white = Image.new("RGB", image.size, "white")
        white.paste(image, mask=image.getchannel("A"))
        white.save(dst, "PDF", resolution=float(dpi))


def placement(width, height, box, fit):
    x, y, w, h = box
    scale = 1 if fit == "actual" else (max if fit == "fill" else min)(w / width, h / height)
    nw, nh = width * scale, height * scale
    return (x + (w - nw) / 2, y + (h - nh) / 2, x + (w + nw) / 2, y + (h + nh) / 2)


def layout(src, dst, kind, options, media):
    import fitz
    with fitz.open(src) as doc, fitz.open() as out:
        selected = page_range(options["pages"], len(doc))
        n = options["slides"] if kind in SLIDES else 1
        cols, rows = {1: (1, 1), 2: (2, 1), 3: (1, 3), 4: (2, 2), 6: (3, 2), 9: (3, 3)}[n]
        for start in range(0, len(selected), n):
            first = doc[selected[start]].rect
            landscape = (first.width > first.height if n == 1 else n != 3) if options["orientation"] == "auto" else options["orientation"] == "landscape"
            w, h = PAPER[media]
            if landscape:
                w, h = h, w
            page = out.new_page(width=w, height=h)
            for i, index in enumerate(selected[start:start + n]):
                rect = doc[index].rect
                # A small inset keeps handouts and fitted text inside the Epson's printable area.
                margin = 12
                box = ((i % cols) * w / cols + margin, (i // cols) * h / rows + margin, w / cols - margin * 2, h / rows - margin * 2)
                if options["fit"] == "fill":
                    box = (0, 0, w, h)
                dest = fitz.Rect(placement(rect.width, rect.height, box, options["fit"]))
                if doc[index].get_contents():
                    page.show_pdf_page(dest, doc, index)
        out.save(dst, garbage=4, deflate=True)
        print(f"layout processed={len(selected)}, expected={len(selected)}, sheets={len(out)}", file=sys.stderr)
        return len(doc), len(out)


def thumbnails(pdf, colour=True):
    import fitz
    result = []
    with fitz.open(pdf) as doc:
        for page in list(doc)[:20]:
            edge = 240
            while True:
                png = page.get_pixmap(matrix=fitz.Matrix(edge / max(page.rect.width, page.rect.height), edge / max(page.rect.width, page.rect.height)), colorspace=fitz.csRGB if colour else fitz.csGRAY, alpha=False).tobytes("png")
                encoded = base64.b64encode(png).decode("ascii")
                if len(encoded) <= 24 * 1024:
                    break
                edge //= 2
                if edge < 30:
                    raise ValueError("Preview is too large")
            result.append(encoded)
    if sum(map(len, result)) > MAX_PREVIEW:
        raise ValueError("Preview exceeds 512 KiB")
    return result


def parse_inkcov(text):
    pages = []
    for line in text.splitlines():
        m = re.match(r"\s*([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+CMYK OK", line)
        if m:
            values = [float(v) for v in m.groups()]
            if any(not math.isfinite(v) or not 0 <= v <= 1 for v in values):
                raise ValueError("Invalid ink coverage")
            pages.append(dict(zip(("c", "m", "y", "k"), values)))
    return pages


def run(args, timeout):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def main():
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RASTER, MAX_RASTER))
        job = json.loads((Path(JOB) / "job.json").read_text())
        name, media = job.get("file"), job.get("media")
        if not isinstance(name, str) or not re.fullmatch(r"doc\.(pdf|jpg|png|" + "|".join(OFFICE + ["heic", "heif"]) + ")", name) or media not in PAPER:
            raise ValueError("Invalid paper job")
        kind = "jpeg" if name.endswith(".jpg") else name.split(".")[-1]
        options = options_of(job.get("options"), kind)
        src = str(Path(JOB) / name)
        bounded_file(src, MAX_FILE)
        out = Path(JOB) / "out"
        out.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="paper-") as temp:
            pdf = src
            if kind in OFFICE:
                pdf = f"{temp}/converted.pdf"
                office_convert(src, pdf, kind, options, media, f"{temp}/profile")
            elif kind != "pdf":
                if kind in ("heic", "heif"):
                    jpg = f"{temp}/photo.jpg"
                    r = run(["heif-convert", src, jpg], 45)
                    if r.returncode:
                        raise ValueError("The HEIC photo could not be read")
                    bounded_file(jpg, MAX_PDF)
                    src = jpg
                pdf = f"{temp}/picture.pdf"
                image_to_pdf(src, pdf)
            bounded_file(pdf, MAX_PDF)
            final = f"{temp}/print.pdf"
            source_pages, count = layout(pdf, final, kind, options, media)
            bounded_file(final, MAX_PDF)
            r = run(GS + ["-o", "-", "-sDEVICE=inkcov", final], 120)
            pages = parse_inkcov(r.stdout)
            if r.returncode or len(pages) != count:
                raise ValueError(f"Ink measurement processed={len(pages)}, expected={count}")
            preview = thumbnails(final, job.get("colour") is True) if job.get("preview") is True else None
            pwg = str(out / "doc.pwg")
            r = run(GS + ["-sDEVICE=pwgraster", "-r300", "-dcupsBitsPerColor=8", f"-dcupsColorSpace={19 if job.get('colour') is True else 18}", "-o", pwg, final], 300)
            if r.returncode:
                raise ValueError("The pages could not be rendered within the 512 MB output limit")
            size = bounded_file(pwg, MAX_RASTER)
            result = {"ok": True, "pages": pages, "bytes": size}
            if preview is not None:
                with open(pwg, "rb") as fh:
                    digest = hashlib.file_digest(fh, "sha256").hexdigest()
                result.update(preview=preview, sourcePages=source_pages, renderHash=digest)
            (out / "result.json").write_text(json.dumps(result))
        return 0
    except Exception as e:
        print(f"paper failed: {type(e).__name__}: {e}", file=sys.stderr)
        return fail(str(e) if isinstance(e, ValueError) else "The file could not be prepared within the time and size limits. Try a smaller file or export a PDF.")


def selftest(image=False):
    global JOB
    assert parse_inkcov("0.01 0.00 0.00 0.04 CMYK OK") == [{"c": .01, "m": 0, "y": 0, "k": .04}]
    assert page_range("1-3,5", 5) == [0, 1, 2, 4]
    if image:
        import fitz
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            src = f"{temp}/tiny.docx"
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
                z.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
                z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Paper conversion works</w:t></w:r></w:p></w:body></w:document>')
            office_convert(src, f"{temp}/tiny.pdf", "docx", options_of(None, "docx"), "letter", f"{temp}/profile")
            with fitz.open(f"{temp}/tiny.pdf") as doc:
                assert len(doc) == 1 and "Paper conversion works" in doc[0].get_text()
            Image.new("RGB", (32, 32), "red").save(f"{temp}/tiny.png")
            assert run(["heif-enc", "-o", f"{temp}/tiny.heic", f"{temp}/tiny.png"], 30).returncode == 0
            assert run(["heif-convert", f"{temp}/tiny.heic", f"{temp}/tiny.jpg"], 30).returncode == 0
            image_to_pdf(f"{temp}/tiny.jpg", f"{temp}/photo.pdf")
            with fitz.open(f"{temp}/photo.pdf") as doc:
                assert len(doc) == 1
            for kind, original in (("docx", src), ("heic", f"{temp}/tiny.heic")):
                work = Path(temp) / kind
                work.mkdir()
                shutil.copyfile(original, work / f"doc.{kind}")
                (work / "job.json").write_text(json.dumps(dict(file=f"doc.{kind}", media="letter", colour=False, preview=True)))
                before = JOB
                try:
                    JOB = str(work)
                    hashes = []
                    for _ in range(2):
                        assert main() == 0
                        result = json.loads((work / "out" / "result.json").read_text())
                        assert result["ok"] and result["sourcePages"] == 1 and len(result["pages"]) == len(result["preview"]) == 1, result
                        hashes.append(result["renderHash"])
                    assert hashes[0] == hashes[1], "a repeated conversion must preserve the approval hash"
                finally:
                    JOB = before
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["formats"]:
        print(json.dumps(FORMATS))
    elif sys.argv[1:] in (["selftest"], ["image-selftest"]):
        selftest(sys.argv[1] == "image-selftest")
    elif sys.argv[1:2] == ["office"]:
        office_worker(*sys.argv[2:])
    else:
        sys.exit(main())
