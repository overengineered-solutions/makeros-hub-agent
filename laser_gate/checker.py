"""Job-file checker. Pure: no sockets, no files, no clock.

check(body, max_power_pct, cfg) answers whether a member's LightBurn "Save RD File" output may be stored in the
controller's memory. It refuses anything it does not positively recognise. The first failing rule wins, in the order
of the numbered steps in check(). Offsets are byte offsets in the unswizzled file. Layer numbers in reasons count
from 1; the "index" in the result is the file's own 0-based layer (part) number.

Wire facts (MeerK40t ruida driver, jnweiger/ruida-laser protocol.md, kkaempf/ruida, EduTech wiki):
- a command starts at a byte >= 0x80; its parameter bytes are < 0x80;
- power is 14-bit, percent = v * 100 / 16383; speed is 35-bit in um/s; coordinates are um (absolute: 35-bit read as
  signed 32; relative: signed 14-bit).
"""
import hashlib
import itertools
import re
from array import array
from collections.abc import Sequence
from typing import NamedTuple

# Codec copied from pstation scripts/ruida-readonly.py (_swizzle/_unswizzle, enc14/dec14/decu35; proven against the
# OMTech's controller over USB on 2026-10-12). The UDP framing (2-byte checksum) lives in gate.py.
MAGIC = 0x88


def _swizzle_byte(b):
    b ^= (b >> 7) & 0xFF; b ^= (b << 7) & 0xFF; b ^= (b >> 7) & 0xFF; b ^= MAGIC
    return (b + 1) & 0xFF


def _unswizzle_byte(b):
    b = (b - 1) & 0xFF; b ^= MAGIC; b ^= (b >> 7) & 0xFF; b ^= (b << 7) & 0xFF; b ^= (b >> 7) & 0xFF
    return b


SW = bytes(_swizzle_byte(i) for i in range(256))
USW = bytes(_unswizzle_byte(i) for i in range(256))


def swizzle(data):
    return bytes(data).translate(SW)


def unswizzle(data):
    return bytes(data).translate(USW)


# Opcodes whose next byte is a sub-code; the command is keyed (op, sub). E5 (file upload/sum, research) is keyed too
# so `learn` names it properly; it is not allowed.
SUBCODED = frozenset({0xA5, 0xC6, 0xC9, 0xCA, 0xD8, 0xD9, 0xDA, 0xE5, 0xE6, 0xE7, 0xE8, 0xF1, 0xF2})

# ---------------------------------------------------------------------------------------------------------------------
# TO CONFIRM against the operator's sample .rd files: an entry stays only if a sample uses it; lengths are research
# values; `learn` prints observed ones. (Drop samples in laser_gate/samples/, run `python3 -m laser_gate learn
# laser_gate/samples`, trim this table, run the tests.)
#
# key -> number of parameter bytes. Anything not here is refused.
ALLOWED = {
    # moves and cuts: absolute x(5) y(5); relative dx(2) dy(2); relative dx(2); relative dy(2)
    (0x88,): 10, (0x89,): 4, (0x8A,): 2, (0x8B,): 2,
    (0xA8,): 10, (0xA9,): 4, (0xAA,): 2, (0xAB,): 2,
    # power, laser 1: min, max (2); layer min, max: layer(1) + power(2). Every other C6 needs a sample AND a cap rule.
    (0xC6, 0x01): 2, (0xC6, 0x02): 2, (0xC6, 0x31): 3, (0xC6, 0x32): 3,
    # speed: speed(5); layer speed: layer(1) + speed(5)
    (0xC9, 0x02): 5, (0xC9, 0x04): 6,
    # layer: work mode / layer end (1); layer number (1); layer colour: layer(1) + colour(5); max layer (1)
    (0xCA, 0x01): 1, (0xCA, 0x02): 1, (0xCA, 0x06): 6, (0xCA, 0x22): 1,
    # header marker: exactly once, before the first move or cut (see check step 6)
    (0xD8, 0x00): 0,
    # reference-point mode: machine zero / anchor / current position
    (0xD8, 0x10): 0, (0xD8, 0x11): 0, (0xD8, 0x12): 0,
    # set absolute; block end; bounds (10 = x(5) y(5)); layer bounds (11 = layer(1) + x(5) y(5))
    (0xE6, 0x01): 0, (0xE7, 0x00): 0,
    (0xE7, 0x03): 10, (0xE7, 0x07): 10, (0xE7, 0x50): 10, (0xE7, 0x51): 10,
    (0xE7, 0x52): 11, (0xE7, 0x53): 11, (0xE7, 0x61): 11, (0xE7, 0x62): 11,
    # process repeat (7 x 2), array direction (1), feed repeat (2 x 5): MeerK40t lengths, samples decide
    (0xE7, 0x04): 14, (0xE7, 0x05): 1, (0xE7, 0x06): 10,
    # reference point set; element indexes; block cutting; display offset; feed auto calc
    (0xF0,): 0,
    (0xF1, 0x00): 1, (0xF1, 0x01): 1, (0xF1, 0x02): 1, (0xF1, 0x03): 10, (0xF1, 0x04): 1,
    # element index, name index, mirror (1); element name (10); element array bounds/add (10); element array (7 x 2)
    (0xF2, 0x00): 1, (0xF2, 0x01): 1, (0xF2, 0x07): 1,
    (0xF2, 0x02): 10, (0xF2, 0x03): 10, (0xF2, 0x04): 10, (0xF2, 0x06): 10, (0xF2, 0x05): 14,
    # array start (1), array end
    (0xEA,): 1, (0xEB,): 0,
    # end of file: exactly once, last
    (0xD7,): 0,
}

# Refused by name so the reason reads well. They are refused anyway because they are not in ALLOWED; nothing in this
# package can build them for sending. A key (op,) covers every sub-code of that op.
NEVER = {
    (0xD8, 0x01): "stop", (0xD8, 0x02): "pause", (0xD8, 0x03): "restore",
    (0xD8, 0x2A): "home X/Y", (0xD8, 0x2C): "home Z", (0xD8, 0x2D): "home U", (0xD8, 0x2E): "focus Z",
    (0xD8,): "process control or jog key",
    (0xD9,): "jog/rapid move",
    (0xDA, 0x00): "settings read", (0xDA, 0x01): "settings write", (0xDA,): "settings",
    (0xE8, 0x00): "delete stored file", (0xE8, 0x01): "stored file name", (0xE8, 0x02): "file transfer",
    (0xE8, 0x03): "start stored file", (0xE8,): "stored-file control",
    (0xE7, 0x01): "file name (the gate writes its own)",
    (0xE5,): "file upload control",
    (0xA5,): "panel key",
    (0xCC,): "ack", (0xCD,): "error", (0xCE,): "keep-alive", (0xCF,): "nak",
}
# A trailing DA 01 06 20 ... (a scratch-setting write some writers append) is refused like any DA, until the operator
# decides after seeing samples (contract C10).

# Leading client-envelope command keys to drop before checking (a client's own E8 02 / E7 01, if samples carry one).
ENVELOPE_STRIP = ()

# Repeat and array commands may carry only their do-nothing values (check step 14): check() walks one copy of the path,
# once, so a count above one would run passes or copies (possibly off the bed) that nothing here looked at. Research
# values (MeerK40t's LightBurn-style header): process repeat 1, 1, 0...; feed repeat 0, 0; element array 1, 1, ...;
# array repeat 1, 1, .... Only the leading counts are held; the later fields differ between writers (spacing) and act
# only with a count above one. A sample that is refused for this shows its values in the reason (`learn` prints it).
# key -> (name, leading values from the parameter bytes, the only values allowed)
NEUTRAL = {
    (0xE7, 0x04): ("process repeat", lambda p: (u14(p[0:2]), u14(p[2:4])), (1, 1)),
    (0xE7, 0x06): ("feed repeat", lambda p: (u35(p[0:5]), u35(p[5:10])), (0, 0)),
    # E7 08 is not in ALLOWED; if a sample brings it in, this already holds it
    (0xE7, 0x08): ("array repeat", lambda p: (u14(p[0:2]), u14(p[2:4])), (1, 1)),
    (0xF2, 0x05): ("element array", lambda p: (u14(p[0:2]), u14(p[2:4])), (1, 1)),
}
# ---------------------------------------------------------------------------------------------------------------------

START, END = (0xD8, 0x00), (0xD7,)
MOVES = frozenset({0x88, 0x89, 0x8A, 0x8B, 0xA8, 0xA9, 0xAA, 0xAB})
CUTS = frozenset({0xA8, 0xA9, 0xAA, 0xAB})
POWERS = frozenset({(0xC6, 0x01), (0xC6, 0x02), (0xC6, 0x31), (0xC6, 0x32)})  # min, max; layer min, layer max
SPEEDS = frozenset({(0xC9, 0x02), (0xC9, 0x04)})                              # speed; layer speed
BOUNDS = frozenset({(0xE7, 0x03), (0xE7, 0x07), (0xE7, 0x50), (0xE7, 0x51)})
# Every opcode the research catalogue names (allowed or not). A file most of whose commands fall outside this is not an
# .rd file at all (random bytes, text, the wrong swizzle), rather than an .rd file with one bad command.
KNOWN_OPS = frozenset({0x80, 0x88, 0x89, 0x8A, 0x8B, 0xA0, 0xA5, 0xA8, 0xA9, 0xAA, 0xAB, *range(0xC0, 0xD0),
                       0xD7, 0xD8, 0xD9, 0xDA, 0xE5, 0xE6, 0xE7, 0xE8, 0xEA, 0xEB, 0xF0, 0xF1, 0xF2})

NAME_RE = re.compile(r"[A-Z]{2}[0-9]{4}")


def envelope(name):
    """The gate's own store envelope, UNVERIFIED (T0): E8 02, then E7 01 <name ASCII> 00. Prepended only by /store."""
    if not NAME_RE.fullmatch(name):
        raise ValueError("name must be two capitals and four digits")
    return b"\xE8\x02\xE7\x01" + name.encode("ascii") + b"\x00"


ENVELOPE_LEN = len(envelope("AA0000"))


class Tok(NamedTuple):
    key: tuple  # (op,), (op, sub), or () for parameter bytes with no command in front
    off: int    # offset of the opcode
    ps: int     # first parameter byte
    end: int    # one past the last parameter byte

    @property
    def n(self):
        return self.end - self.ps


_SPAN = re.compile(rb"[\x80-\xff][\x00-\x7f]*|[\x00-\x7f]+")  # one command (opcode + parameter bytes), or a lead-in


def _tok(data, off, end):
    op = data[off]
    if op < 0x80:
        return Tok((), off, off, end)
    if op in SUBCODED and off + 1 < end:
        return Tok((op, data[off + 1]), off, off + 2, end)
    return Tok((op,), off, off + 1, end)


class Tokens(Sequence):
    """The commands of `data`, made into Tok on demand. Only a 4-byte offset per command is held: as a list of Tok, a
    4 MiB file of 1-byte commands took ~750 MB, and the unit's MemoryMax is 128M."""

    def __init__(self, data):
        self.data = data
        self.starts = array("I", (m.start() for m in _SPAN.finditer(data)))

    def __len__(self):
        return len(self.starts)

    def __getitem__(self, i):
        i = range(len(self.starts))[i]  # negative indexes; IndexError past either end
        return _tok(self.data, self.starts[i], self.starts[i + 1] if i + 1 < len(self.starts) else len(self.data))

    def __iter__(self):
        data, starts = self.data, self.starts
        for off, end in zip(starts, itertools.chain(itertools.islice(starts, 1, None), (len(data),))):
            yield _tok(data, off, end)


def tokenize(data):
    return Tokens(data)


def op_str(key):
    return " ".join(f"{b:02X}" for b in key) or "(no command)"


def never_name(key):
    return NEVER.get(key) or NEVER.get(key[:1])


def enc14(v):
    return bytes([(v >> 7) & 0x7F, v & 0x7F])


def u14(p):
    return (p[0] & 0x7F) << 7 | (p[1] & 0x7F)


def s14(p):
    v = u14(p)
    return v - 0x4000 if v > 0x1FFF else v


def u35(p):
    v = 0
    for b in p[:5]:
        v = v << 7 | (b & 0x7F)
    return v


def s32(p):  # absolute coordinate: 35 bits on the wire, read as signed 32 (MeerK40t abscoord)
    v = u35(p) & 0xFFFFFFFF
    return v - 0x100000000 if v > 0x7FFFFFFF else v


def pct(p):
    return u14(p) * 100 / 16383


POWER_STEP = 100 / 16383  # one 14-bit power step, in percent


def job_bytes(body):
    """Unswizzled job with any ENVELOPE_STRIP lead-in removed -> (data, offset of data in the file)."""
    data, cut = unswizzle(body), 0
    for m in _SPAN.finditer(data):  # lazily: this stops at the first command that isn't envelope
        if _tok(data, m.start(), m.end()).key not in ENVELOPE_STRIP:
            break
        cut = m.end()
    return data[cut:], cut


def _mm(um):
    return round(um / 1000, 1)


def check(body, max_power_pct, cfg):
    """-> {"verdict": "checked", ...} or {"verdict": "refused", "code", "reason", "offset", ...}.

    cfg needs max_file_bytes, packet_max_bytes, min_power_pct (0 = not calibrated: the minimum is skipped here; the
    HTTP layer answers not_calibrated before calling), max_power_pct, bed_mm, speed_mm_s."""
    sha, size = hashlib.sha256(body).hexdigest(), len(body)

    def refuse(code, reason, offset=None):
        return {"verdict": "refused", "code": code, "reason": reason, "offset": offset, "sha256": sha, "sizeBytes": size}

    data, base = job_bytes(body)
    toks = tokenize(data)
    at = lambda t: base + t.off  # noqa: E731

    # 1. not_rd
    if not toks or not toks[0].key or toks[0].key[0] not in KNOWN_OPS \
            or 2 * sum(1 for t in toks if t.key and t.key[0] in KNOWN_OPS) < len(toks):
        return refuse("not_rd", "this isn't a Ruida .rd file")
    # 2. too_large
    if size > cfg.max_file_bytes:
        return refuse("too_large", f"the file is {size} bytes; the limit is {cfg.max_file_bytes}")
    # 3. truncated: the last command is missing its sub-code or some of its parameter bytes
    last = toks[-1]
    if (last.key[0] in SUBCODED and len(last.key) == 1 and last.n == 0) or last.n < ALLOWED.get(last.key, 0):
        return refuse("truncated", f"the file ends in the middle of command {op_str(last.key)} (byte {at(last)})", at(last))
    # 4. command_not_allowed (a second D8 00 counts: the marker is allowed exactly once)
    starts = 0
    for t in toks:
        starts += t.key == START
        if t.key not in ALLOWED or (t.key == START and starts > 1):
            why = "a second header marker" if t.key == START else never_name(t.key)
            return refuse("command_not_allowed", f"command {op_str(t.key)} at byte {at(t)} isn't allowed"
                          + (f" ({why})" if why else ""), at(t))
    # 5. bad_length
    for t in toks:
        if t.n != ALLOWED[t.key]:
            return refuse("bad_length", f"command {op_str(t.key)} at byte {at(t)} has {t.n} parameter bytes; "
                          f"it takes {ALLOWED[t.key]}", at(t))
    # 6. no_header_marker: D8 00 must come before the first move or cut
    start_i = next((i for i, t in enumerate(toks) if t.key == START), None)
    move_i = next((i for i, t in enumerate(toks) if t.key[0] in MOVES), len(toks))
    if start_i is None or start_i > move_i:
        return refuse("no_header_marker", "the header has no D8 00")
    # 7. header_too_long: envelope + header (through D8 00) is store's first packet
    header = ENVELOPE_LEN + toks[start_i].end
    if header > cfg.packet_max_bytes:
        return refuse("header_too_long", f"the header is {header} bytes; it has to fit in one "
                      f"{cfg.packet_max_bytes}-byte packet")
    # 8. no_end, 9. data_after_end
    if last.key != END:
        return refuse("no_end", "the file doesn't end with D7")
    for t in itertools.islice(toks, len(toks) - 1):
        if t.key == END:
            return refuse("data_after_end", f"there is data after the end mark (D7) at byte {at(t)}", at(t))

    # 10-12 and 12b in one pass that keeps per-layer summaries only: a tuple per power command once took ~212 MiB for a
    # 2 MiB file of them, over the unit's MemoryMax=128M. Values are unrounded: the limits compare these, and only
    # reasons and the result round. C6 01/02 and C9 02 apply to the layer set by the last CA 02. The first offender of
    # each rule is kept, and the rules still answer in step order.
    cap = min(max_power_pct, cfg.max_power_pct)
    lo, hi = cfg.speed_mm_s
    first = {}     # code -> (reason, offset)
    by_layer = {}  # layer -> the result's summary
    have = {}      # layer -> the settings it has had so far: "max power", "min power", "speed"
    layer, any_max, any_cut = 0, False, False
    for t in toks:
        p, k = data[t.ps:t.end], t.key
        if k == (0xCA, 0x02):
            layer = p[0]
            continue
        if k in POWERS or k in SPEEDS:
            n, p = (layer, p) if k in ((0xC6, 0x01), (0xC6, 0x02), (0xC9, 0x02)) else (p[0], p[1:])
            e = by_layer.setdefault(n, {"index": n, "maxPowerPct": None, "minPowerPct": None, "speedMmS": None})
        if k in POWERS:
            v, is_max = pct(p), k in ((0xC6, 0x02), (0xC6, 0x32))
            # 10. power_over_cap: every power value, min and max, is held under the cap (min above max would fire
            #     hotter). Within one 14-bit step of a limit counts as at it: writers round a percent differently.
            if v > cap + POWER_STEP:
                first.setdefault("power_over_cap", (f"layer {n + 1} power {v:.1f}% is over the {cap:.1f}% cap", at(t)))
            # 11. power_under_min: max-power values only (min power is the corner power)
            if is_max and cfg.min_power_pct > 0 and v < cfg.min_power_pct - POWER_STEP:
                first.setdefault("power_under_min", (f"layer {n + 1} power {v:.1f}% is under the "
                                 f"{cfg.min_power_pct:.1f}% minimum (below it the exhaust fans don't start)", at(t)))
            any_max |= is_max
            f, pick = ("maxPowerPct", max) if is_max else ("minPowerPct", min)
            e[f] = round(v, 1) if e[f] is None else pick(e[f], round(v, 1))
            have.setdefault(n, set()).add("max power" if is_max else "min power")
        elif k in SPEEDS:
            v = u35(p)
            # 12. speed_out_of_range
            if not lo * 1000 <= v <= hi * 1000:
                first.setdefault("speed_out_of_range", (f"layer {n + 1} speed {v / 1000:.1f} mm/s is outside "
                                 f"{lo:g}–{hi:g} mm/s", at(t)))
            e["speedMmS"] = round(v / 1000, 1) if e["speedMmS"] is None else max(e["speedMmS"], round(v / 1000, 1))
            have.setdefault(n, set()).add("speed")
        elif k[0] in CUTS:
            any_cut = True
            # 12b. settings_missing: a cut on a layer with no power or speed set before it would run at whatever the
            #      controller last held, which nothing here checked.
            missing = [w for w in ("max power", "min power", "speed") if w not in have.get(layer, ())]
            if missing:
                first.setdefault("settings_missing", (f"layer {layer + 1} cuts at byte {at(t)} with no "
                                 f"{' or '.join(missing)} setting before it", at(t)))
    # 11, again: a file that cuts with no max power setting at all
    if cfg.min_power_pct > 0 and any_cut and not any_max:
        first.setdefault("power_under_min", (f"layer 1 power 0.0% is under the {cfg.min_power_pct:.1f}% minimum "
                                             "(below it the exhaust fans don't start)", None))
    for code in ("power_over_cap", "power_under_min", "speed_out_of_range", "settings_missing"):
        if code in first:
            return refuse(code, *first[code])
    # 13. out_of_bed. Every head position (moves and cuts; relative steps accumulate from the last absolute point, or
    #     from 0,0 before one) and every E7 03/07/50/51 bound must lie in [0, bed]. Every point inside the bed means the
    #     overall extent fits too. ponytail: with "current position" reference mode (D8 12) the controller adds the
    #     head's position at Start, which no file check can see; the controller's own soft limits cover that case.
    w, h = (round(v * 1000) for v in cfg.bed_mm)
    x = y = 0
    cut = None  # [min_x, min_y, max_x, max_y] of cutting segments
    for t in toks:
        p, op = data[t.ps:t.end], t.key[0]
        if t.key in BOUNDS:
            px, py = s32(p[0:5]), s32(p[5:10])
        elif op in MOVES:
            ox, oy = x, y
            if op in (0x88, 0xA8):
                x, y = s32(p[0:5]), s32(p[5:10])
            elif op in (0x89, 0xA9):
                x, y = x + s14(p[0:2]), y + s14(p[2:4])
            elif op in (0x8A, 0xAA):
                x += s14(p)
            else:
                y += s14(p)
            if op in CUTS:
                cut = cut or [ox, oy, ox, oy]
                cut[:] = [min(cut[0], ox, x), min(cut[1], oy, y), max(cut[2], ox, x), max(cut[3], oy, y)]
            px, py = x, y
        else:
            continue
        if not (0 <= px <= w and 0 <= py <= h):
            return refuse("out_of_bed", f"a path leaves the bed ({_mm(px):.1f}, {_mm(py):.1f} mm is outside "
                          f"{_mm(w):.1f} × {_mm(h):.1f} mm)", at(t))
    # 14. repeat_or_array: one copy, run once (see NEUTRAL)
    for t in toks:
        if t.key in NEUTRAL:
            name, values, want = NEUTRAL[t.key]
            got = values(data[t.ps:t.end])
            if got != want:
                return refuse("repeat_or_array", f"command {op_str(t.key)} at byte {at(t)} sets {name} to "
                              f"{', '.join(map(str, got))}; only {', '.join(map(str, want))} (one copy, run once) is "
                              "allowed", at(t))

    cut = cut or [0, 0, 0, 0]
    return {"verdict": "checked", "sha256": sha, "sizeBytes": size, "maxPowerPct": cap,
            "widthMm": _mm(cut[2] - cut[0]), "heightMm": _mm(cut[3] - cut[1]),
            "layers": [by_layer[n] for n in sorted(by_layer)][:64]}


def plan_packets(body, name, packet_max_bytes):
    """A checked file -> unswizzled store packets: [envelope + header through D8 00, then <= packet_max_bytes chunks
    cut only between commands]. Call only after check() said "checked"."""
    data, _ = job_bytes(body)
    toks = tokenize(data)
    hdr = next(t.end for t in toks if t.key == START)
    packets, cur = [envelope(name) + data[:hdr]], b""
    for t in toks:
        if t.off < hdr:
            continue
        cmd = data[t.off:t.end]
        if cur and len(cur) + len(cmd) > packet_max_bytes:
            packets.append(cur)
            cur = b""
        cur += cmd
    if cur:
        packets.append(cur)
    return packets
