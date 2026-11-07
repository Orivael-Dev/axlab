# Adding a research track

AxLab holds many research tracks. Each one is a folder under `experiments/`
that someone else can run and check without asking us anything.

## Layout

```text
experiments/<track>/
  README.md        the question, how to run it, how to read the numbers
  bench.py         the experiment: writes results/<name>.json with the host
  results/         raw results from the runs the README or note quotes
  figures/         optional: generated from results/, never drawn by hand
  note.html        optional: the published note, if there is one
```

Code the experiment measures goes under `axlab/<area>/`, not inside the
track folder, so tests can cover it and other tracks can reuse it.

## Checklist for a new track

- [ ] **Provenance:** if code comes from AXIOM, add it to the file map in
      [PROVENANCE.md](PROVENANCE.md) with the source commit and every change.
      Copy files; never merge private history.
- [ ] **Audit:** read every file you add. No secrets, keys, tokens, customer
      data, internal hostnames or deployment addresses. Result files and logs
      shouldn't contain absolute paths or user names.
- [ ] **No network by default:** the default run makes no network calls and
      no paid model requests. A real model is an opt-in flag that points at
      something the user runs locally.
- [ ] **Keys:** nothing ships with a fixed signing key. A benchmark may set a
      random per-run key when none is set (see `experiments/slim_fabric/bench.py`).
- [ ] **Tests:** add tests under `tests/` for the code under `axlab/`. They
      must pass with no AXIOM install and no network.
- [ ] **Label the numbers:** measured, simulated, or projected. Record the
      host in every result file. Say what the experiment does *not* show.
- [ ] **Results:** commit the raw JSON behind every number you quote. Local
      runs go in `results/local/` (git-ignored) until you choose to publish.
- [ ] **README:** add the track to the table in the top-level README.
- [ ] **Tier:** if the track might become paid later, say so in its README
      now. Everything in this repo is under the noncommercial license.
