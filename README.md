# car_pvr_verify

Checks that CA-R files were copied completely and correctly at the
**Preservation Version Requested (PVR)** step. It replaces building file lists
by hand and comparing them in a spreadsheet.

It covers three jobs:

| Job | Stream | What it compares |
|---|---|---|
| `interim` | DG | backup source drives or folders → the Write2LTO interim drive |
| `partner` | AV | source inventory → vendor drive → partner drive |
| `tape` | LTO | interim drive inventory + pre-write inventories → post-write inventories of tape A and tape B, and A ↔ B |

The tool is **read-only**:

- It reads file names and sizes only and never opens a file's contents.
  Checksums stay with `md5tool.py`.
- It **never reads a live tape**. Tapes are checked from `tape-inventory.py`
  lists. If you give it a folder on an LTFS mount, it refuses.
- It never changes, moves, deletes or `dot_clean`s anything it compares.
- Reports go only to the folder you choose. The one exception is the optional
  partner-drive inventory (see below), which is written only when you ask for
  it and confirm.

## Install

```
git clone https://github.com/carevealed/car_pvr_verify.git
cd car_pvr_verify
python3 car_pvr_verify.py --selftest
```

Or download `car_pvr_verify.py` on its own: it is a single file.

## Requirements

- Python 3.8 or newer (macOS or Linux)
- `openpyxl`, only if you want the `.xlsx` workbook: `pip install -r requirements.txt`
- `tape-inventory.py` that lists **every** file, `.md5` sidecars included, for the tape job

## Quick start

```
python3 car_pvr_verify.py
```

With no arguments the tool asks what to check and then asks for each input.
You can drag folders and files into the Terminal window. Quotes, backslash
escapes and trailing spaces are cleaned up for you. Every run asks where to
save the reports; press Enter to use the current folder.

To check the tool on your computer first:

```
python3 car_pvr_verify.py --selftest
```

### interim (DG)

```
python3 car_pvr_verify.py interim \
    --source /Volumes/DG_BACKUP_1 --source /Volumes/DG_BACKUP_2 \
    --dest /Volumes/CAR_043_12T --within Write2CAVPP0060
```

Several grant cycles can be merged into one MARC folder. If the same path turns
up in more than one source, it is reported as a **duplicate across sources**.
The tool doesn't pick one.

### partner (AV)

```
python3 car_pvr_verify.py partner \
    --source-list capehlm_source_inventory.tsv \
    --vendor /Volumes/MP_DRIVE --partner /Volumes/capehlm_GG \
    --all-parents --write-inventory
```

This runs two comparisons: source inventory → vendor drive, then vendor drive →
partner drive. All three must match exactly.

- **Source inventory:** a `tape-inventory.py`-style list stands in for the
  MediaPreserve drive directory `.txt` until that format is added.
- **MARC folders in several places:** MediaPreserve drives split MARC folders by
  original format (film, audio, video). Use `--all-parents` to merge them.
- **Drive-level files:** the ReadMe, the metadata CSV and `Md5tool/` sit outside
  the MARC folders. They are listed in `not_compared.csv` and are not counted as
  discrepancies.
- **`--write-inventory`:** after checking, writes
  `<drive name>_Inventory_<date>.csv` to the root of the partner drive. Its
  columns are marc_code, object_identifier, path, filename, size_bytes and md5,
  with the md5 read from each file's sidecar. The tool asks first (`--yes`
  answers for you), never overwrites an existing file, and skips writing if the
  drive is read-only.

### tape (LTO)

```
python3 car_pvr_verify.py tape \
    --source CAR_043_12T_Inv_Source4CAVPP0060L8Write2_2026-11-04.tsv \
    --a-pre  CAVPP0060L8A_Inv_PreWrite2_2026-11-04.tsv \
    --a-post CAVPP0060L8A_Inv_PostWrite2_2026-11-05.tsv \
    --b-pre  CAVPP0060L8B_Inv_PreWrite2_2026-11-05.tsv \
    --b-post CAVPP0060L8B_Inv_PostWrite2_2026-11-06.tsv \
    --within Write2CAVPP0060
```

All tape inputs must be `tape-inventory.py` lists. The source can be a list or
the interim folder. The job runs three checks:

1. **Tape A post-write** must hold exactly: everything on A's pre-write list,
   plus everything in the source.
2. **Tape B post-write**, checked the same way against B's own pre-write list.
3. **A post-write and B post-write** must mirror each other.

Rules for these checks:

- **Earlier files:** files on the pre-write list must still be there at the same
  size.
- **Repeated files:** a path that is both on the pre-write list and in the source
  is reported as a duplicate, because this write repeats an earlier one.
- **First write to a tape:** use `--a-first-write` / `--b-first-write`. The
  post-write list must then match the source exactly.
- **Older inventories:** lists made before `tape-inventory.py` listed `.md5`
  files have none. In that case `.md5` files are left out of that comparison,
  and the report says so.
- **Volume names:** the tool warns if a pre-write and post-write list come from
  different volumes, or if the A and B lists share a volume name.

## How files are matched

- **Layout:** CA-R files sit in `<marc_code>/<object_identifier>/<filename>`.
  - Files are compared by their path from the MARC folder down, so different
    drive names, mount points or parent folders don't matter.
  - The MARC code and object identifier come from the folder names, never from
    the filename. Identifiers of any digit length work, including older
    5-digit ones.
- **Finding MARC folders:** a MARC folder is a lowercase folder starting with
  "c" (for example `casfjazz`) that holds at least one object folder named
  `<marc>_<number>`. The tool finds MARC folders at any depth and never looks
  inside one. If it finds them in more than one place, it asks which to use.
  - `--within FOLDER` picks the place inside that folder, which is the usual
    choice for the `Write2…` folder on an interim drive.
  - `--all-parents` uses every place.
- **Files directly in a MARC folder,** such as XML export logs, are compared
  too. The summary lists them as "(files directly in the MARC folder)".
- **Names:** Unicode is normalized (NFC), so macOS NFD names match NFC names.
  Names that differ only in upper/lower case are flagged.
- **Junk:** hidden and junk files are skipped and counted. The defaults are
  `.DS_Store`, `._*`, `.Trashes`, `.Spotlight-V100`, `.fseventsd`, `*.pkf`,
  `$RECYCLE.BIN` and `System Volume Information`. Add more with `--ignore`, or
  start from nothing with `--no-default-ignore`.
- **Order of checks:** presence, then size. If either side has no sizes (for
  example a plain path list), size is not checked, and the report says how many
  files that affected.

### Accepted inputs

Each input can be:

- a live folder or drive (never a tape)
- a `tape-inventory.py` TSV (`SizeinBytes<TAB>FullFilePath`)
- a `size<TAB>path` list with no header (for example `find -printf "%s\t%p\n"`)
- a CSV or TSV with a header that has a path column, plus a size column if it has one
- a plain list of paths, one per line (for example `find` output)

Lists that aren't valid UTF-8 are read as Windows-1252, and the report notes it.

## Reports

Each run makes a folder,
`car_pvr_verify_<job>_<label>_<date>_<time>`, in the location you choose:

| File | Contents |
|---|---|
| `summary.csv` | One row per comparison total, MARC code and object. Each row has file counts and total size (bytes, and GB where 1 GB = 1,000,000,000 bytes) for both sides, a count per category, and OK/CHECK |
| `matched.csv` | Matched files |
| `missing_from_destination.csv` | In the source but not on the destination (for A ↔ B: only on A) |
| `extra_in_destination.csv` | On the destination but not in the source (for A ↔ B: only on B) |
| `size_mismatch.csv` | Same path, different size |
| `duplicate_across_sources.csv` | Same path more than once on one side, with one row per copy |
| `case_conflict.csv` | Names that differ only in upper/lower case |
| `unreadable.csv` | Files or folders that could not be read, or list lines that could not be parsed |
| `not_compared.csv` | Files outside a MARC folder, or in a MARC folder location you didn't select |
| `run_info.txt` | The inputs, ignore list, junk counts and the on-screen summary |
| `….xlsx` | With `--xlsx`: all of the above in one workbook |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Everything matched |
| 1 | Discrepancies found (missing, extra, size, duplicate, case) |
| 2 | Input or usage error, including refusing a live tape |
| 3 | Read errors: something could not be read, so the results are incomplete. This takes priority over 1. |
| 130 | Interrupted (Ctrl-C) |

## Notes and limits

- The MediaPreserve drive directory `.txt` format isn't supported yet. Use a
  source inventory in its place for now.
- The tool keeps one small entry per file in memory: about a few hundred bytes,
  so a million files needs well under 1 GB. It never reads file contents, so the
  size of the drive doesn't matter.

## License

MIT. See [LICENSE](LICENSE).
