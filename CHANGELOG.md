# Changelog

## 1.0.0 - 2026-11-09

First release.

- `interim` job (DG): backup source drives or folders vs the Write2LTO interim
  drive, with duplicates across sources reported rather than picked.
- `partner` job (AV): source inventory vs vendor drive vs partner drive, with an
  optional inventory CSV written to the partner drive root (asks first).
- `tape` job (LTO): post-write = pre-write + source for tape A and tape B, and
  A post-write mirrors B post-write. Works from `tape-inventory.py` lists only and
  refuses live LTFS tapes.
- Accepts live folders, `tape-inventory.py` TSVs, `size<TAB>path` lists,
  CSV/TSV with a path column, and plain path lists.
- MARC folders found by folder name at any depth; `--within` and
  `--all-parents` for drives with MARC folders in more than one place.
- Unicode normalization (NFC), case-only name conflicts, configurable junk list
  with skipped counts.
- Summary CSV (files and GB by comparison, MARC code and object), a detail CSV
  per category, `run_info.txt`, and optional `.xlsx`.
- Exit codes 0 / 1 / 2 / 3 / 130 for workflow gating.
- `--selftest` builds temporary fixtures and checks every category and exit code.
- GitHub Actions runs the self-test on Linux and macOS (Python 3.8 and 3.12).
