"""python3 -m laser_gate serve --config PATH | check FILE --max-power N [--config PATH] | learn DIR"""
import argparse
import collections
import json
import pathlib
import sys

from .checker import ALLOWED, check, op_str, tokenize, unswizzle
from .gate import Config, ConfigError, load_config, serve


def cmd_check(path, max_power, config):
    """Prints what POST /check would answer. No network."""
    cfg = load_config(config, need_token=False)[0] if config else Config()
    if not 1 <= max_power <= 100:
        return print(json.dumps({"error": "bad_request", "detail": "maxPowerPct"})) or 2
    if cfg.min_power_pct <= 0:
        return print(json.dumps({"error": "not_calibrated", "detail": "min_power_pct is not set (T5)"})) or 3
    v = check(pathlib.Path(path).read_bytes(), max_power, cfg)
    print(json.dumps(v, indent=1))
    return 0 if v["verdict"] == "checked" else 1


def cmd_learn(directory):
    """Every (op[, sub]) across DIR/*.rd with counts and observed parameter lengths; flags what ALLOWED lacks."""
    files = sorted(pathlib.Path(directory).glob("*.rd"))
    if not files:
        return print(f"no .rd files in {directory}") or 1
    count, lengths, where = collections.Counter(), collections.defaultdict(collections.Counter), {}
    for f in files:
        for t in tokenize(unswizzle(f.read_bytes())):
            count[t.key] += 1
            lengths[t.key][t.n] += 1
            where.setdefault(t.key, f.name)
    print(f"{len(files)} file(s): {', '.join(f.name for f in files)}\n")
    print(f"{'command':10} {'count':>7}  {'param bytes seen':24} verdict")
    for key in sorted(count):
        seen = ", ".join(f"{n}x{c}" if c > 1 else str(n) for n, c in sorted(lengths[key].items()))
        if key not in ALLOWED:
            verdict = f"NOT ALLOWED (first in {where[key]})"
        elif set(lengths[key]) != {ALLOWED[key]}:
            verdict = f"LENGTH DIFFERS: ALLOWED says {ALLOWED[key]}"
        else:
            verdict = "ok"
        print(f"{op_str(key):10} {count[key]:>7}  {seen:24} {verdict}")
    unused = [op_str(k) for k in ALLOWED if k not in count]
    print(f"\nin ALLOWED but no sample uses it (trim candidates): {', '.join(unused) or 'none'}")
    print("\nverdicts at maxPowerPct 100, bed/speed defaults, no minimum:")
    for f in files:
        v = check(f.read_bytes(), 100, Config())
        print(f"  {f.name}: {v['verdict']}" + (f" {v['code']}: {v['reason']}" if v["verdict"] != "checked" else
              f" {v['widthMm']} x {v['heightMm']} mm, {len(v['layers'])} layer(s)"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m laser_gate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--config", required=True)
    c = sub.add_parser("check")
    c.add_argument("file")
    c.add_argument("--max-power", type=int, required=True)
    c.add_argument("--config")
    lr = sub.add_parser("learn")
    lr.add_argument("dir")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "serve":
            return serve(a.config)
        if a.cmd == "check":
            return cmd_check(a.file, a.max_power, a.config)
        return cmd_learn(a.dir)
    except ConfigError as e:
        print(f"laser-gate: refusing to start: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
