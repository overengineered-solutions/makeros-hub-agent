# Operator sample .rd files

The checker's allowlist (`ALLOWED` in `laser_gate/checker.py`) comes from public research on the Ruida protocol. It
has not yet been compared with a real file from the shop. These samples are what confirm it.

1. On the shop laptop, in LightBurn, save three small jobs with **File → Save RD File**. No laser connection is needed.
   - A cut: a 20 mm square.
   - A score: a line or some text at low power.
   - An engrave: a small filled shape or image.
   Save each as `cut.rd`, `score.rd` and `engrave.rd`, or any names ending in `.rd`.
2. Drop them in this folder.
3. From the repo root, run `python3 -m laser_gate learn laser_gate/samples`. For every command in the samples it
   prints:
   - how many times the command appears;
   - the parameter lengths it was seen with;
   - whether `ALLOWED` lacks it (`NOT ALLOWED`) or expects a different length (`LENGTH DIFFERS`).

   It also lists the `ALLOWED` entries that no sample uses, and how each sample checks.
4. Trim `ALLOWED` to what the samples use. Fix any length the samples disagree on. Don't add a command just because a
   sample carries it: anything new needs a review, and a new power command (`C6 xx`) also needs a cap rule in
   `check()`. Pay special attention to:
   - any `DA 01 …` tail, which is a settings write (contract C10);
   - any leading `E8`/`E7 01`, a client envelope that would go into `ENVELOPE_STRIP`.
5. Run the tests: `python3 -m pytest -q tests/test_laser_gate_checker.py tests/test_laser_gate_store.py tests/test_laser_gate_server.py`.
   G2 checks every `*.rd` here at maxPowerPct 100.

These are design files, not secrets. Even so, commit them only if the designs are the shop's own.
