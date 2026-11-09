#!/usr/bin/env python3
"""car_pvr_verify.py -- check that CA-R files were copied completely and correctly
at the Preservation Version Requested (PVR) step.

Three jobs:
  interim   DG: backup source drives or folders  ->  Write2LTO interim drive
  partner   AV: source inventory  ->  vendor drive  ->  partner drive
  tape      LTO: interim drive inventory + pre-write inventories
            ->  post-write inventories of tape A and tape B, and A <-> B

What it compares: file paths relative to the MARC folder
(<marc_code>/<object_identifier>/<filename>), then file sizes.

The tool is read-only. It reads names and sizes only and never opens a file's
contents (checksums stay with md5tool.py). It never reads a live LTFS tape: tapes
are checked from tape-inventory.py lists. It never changes, moves or deletes
anything it compares. The only file it writes outside the report folder is the
optional partner-drive inventory, and only after you confirm.

Run with no arguments to be asked for everything, or see --help.
Run --selftest to check the tool on this computer.
"""

import argparse
import codecs
import contextlib
import csv
import datetime
import fnmatch
import io
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict

__version__ = "1.0.0"
TOOL = "car_pvr_verify.py"

# ---------------------------------------------------------------------------
# Exit codes (for workflow gating)
# ---------------------------------------------------------------------------
EXIT_OK = 0            # everything matched
EXIT_DISCREPANCY = 1   # missing / extra / size / duplicate / case discrepancies
EXIT_USAGE = 2         # bad or missing input, refused input, nothing to compare
EXIT_READ_ERROR = 3    # a file or folder could not be read (results incomplete)
EXIT_INTERRUPTED = 130 # stopped with Ctrl-C

EXIT_MEANING = {
    EXIT_OK: "everything matched",
    EXIT_DISCREPANCY: "discrepancies found",
    EXIT_USAGE: "input or usage error",
    EXIT_READ_ERROR: "read errors: some files or folders could not be read, so results are incomplete",
    EXIT_INTERRUPTED: "interrupted",
}

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
# Hidden and junk files. Matched against every folder and file name, ignoring
# upper/lower case. Add more with --ignore, or start from nothing with
# --no-default-ignore.
DEFAULT_IGNORE = [
    ".DS_Store",
    "._*",
    ".Trashes",
    ".Spotlight-V100",
    ".fseventsd",
    "*.pkf",
    "$RECYCLE.BIN",
    "System Volume Information",
]

# Tape mount folders used by tape-mount.sh. Anything in these is refused.
KNOWN_TAPE_MOUNTS = ["/media/tape-lto8", "/media/tape-lto6"]

# A MARC folder is a lowercase folder starting with "c" (CA-R MARC codes are the
# lowercase LoC codes, all starting with "c") that holds at least one object
# folder named <marc>_<something>.
MARC_RE = re.compile(r"^c[a-z]*$")

TAPE_INVENTORY_HEADER = "sizeinbytes\tfullfilepath"
PATH_COLUMNS = ("fullfilepath", "fullpath", "filepath", "path", "relativepath", "sourcefile", "file")
SIZE_COLUMNS = ("sizeinbytes", "sizebytes", "size", "bytes", "filesize", "length")

CATEGORIES = [
    ("matched", "Matched"),
    ("missing_from_destination", "Missing from destination"),
    ("extra_in_destination", "Extra in destination"),
    ("size_mismatch", "Size mismatch"),
    ("duplicate_across_sources", "Duplicate across sources"),
    ("case_conflict", "Names differ only in case"),
    ("unreadable", "Unreadable or permission error"),
]
CAT_LABEL = dict(CATEGORIES)
COMPARE_CATS = [c for c, _ in CATEGORIES if c != "unreadable"]
DISCREPANCY_CATS = ("missing_from_destination", "extra_in_destination", "size_mismatch",
                    "duplicate_across_sources", "case_conflict")

DETAIL_COLUMNS = ["comparison", "category", "marc_code", "object_identifier", "relative_path",
                  "expected_side", "expected_size", "destination_side", "destination_size",
                  "size_difference", "expected_full_path", "destination_full_path", "note"]
UNREADABLE_COLUMNS = ["input", "full_path", "problem"]
NOT_COMPARED_COLUMNS = ["input", "reason", "full_path", "size_bytes"]
SUMMARY_COLUMNS = ["comparison", "level", "marc_code", "object_identifier",
                   "expected_side", "expected_files", "expected_bytes", "expected_GB",
                   "destination_side", "destination_files", "destination_bytes", "destination_GB",
                   "matched", "missing_from_destination", "extra_in_destination", "size_mismatch",
                   "duplicate_across_sources", "case_conflict", "status"]

INVENTORY_COLUMNS = ["marc_code", "object_identifier", "path", "filename", "size_bytes", "md5"]

# Indirection so --selftest can simulate permission errors on any platform.
_SCANDIR = os.scandir
_LSTAT = os.lstat

LAST_REPORT_DIR = None  # set by run(); used by --selftest


class UsageError(Exception):
    """Bad, missing or refused input. Exit code 2."""


class UI:
    interactive = bool(sys.stdin) and sys.stdin.isatty()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def nfc(s):
    return unicodedata.normalize("NFC", s)


def gb(nbytes):
    return nbytes / 1e9


def fmt_gb(nbytes):
    return "{0:,.3f} GB".format(gb(nbytes))


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "run"


def clean_path(raw):
    """Clean a typed or drag-and-dropped path.

    Strips surrounding quotes, backslash escapes (macOS Terminal escapes spaces
    as '\\ '), and leading/trailing spaces.
    """
    if raw is None:
        return ""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1]
    elif os.name != "nt" and "\\" in s:
        s = re.sub(r"\\(.)", r"\1", s)
    s = s.strip()
    return os.path.expanduser(s) if s else ""


def ask(question, what, default=""):
    if not UI.interactive:
        raise UsageError("Missing {0}. Give it on the command line (see --help).".format(what))
    try:
        answer = input(question)
    except EOFError:
        raise UsageError("Missing {0}.".format(what))
    answer = answer.strip()
    return answer if answer else default


def ask_path(question, what, allow_blank=False, want=None):
    """Prompt until we get an existing path. want: None, 'file' or 'dir'."""
    while True:
        p = clean_path(ask(question, what))
        if not p:
            if allow_blank:
                return None
            print("  Please drag in a folder or file, or type its path.")
            continue
        if not os.path.exists(p):
            print("  Can't find: {0}".format(p))
            continue
        if want == "file" and not os.path.isfile(p):
            print("  That is a folder. Please give a list file (a tape-inventory.py .tsv).")
            continue
        return os.path.abspath(p)


def ask_yes(question, what):
    return ask(question, what).lower() in ("y", "yes")


class Progress:
    """Live counter on stderr, only when it is a terminal."""

    def __init__(self, label, unit):
        self.label, self.unit = label, unit
        self.live = bool(sys.stderr) and sys.stderr.isatty()

    def tick(self, n):
        if self.live and n % 2000 == 0:
            sys.stderr.write("\r  {0}: {1:,} {2}...".format(self.label, n, self.unit))
            sys.stderr.flush()

    def done(self):
        if self.live:
            sys.stderr.write("\r" + " " * 78 + "\r")
            sys.stderr.flush()


# ---------------------------------------------------------------------------
# Never touch a live tape
# ---------------------------------------------------------------------------

def _unescape_mount(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def parse_proc_mounts(text):
    """Parse Linux /proc/mounts into (source, mount point, fs type)."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            out.append((_unescape_mount(parts[0]), _unescape_mount(parts[1]), parts[2]))
    return out


_MAC_MOUNT_RE = re.compile(r"^(.*?) on (.*) \(([^,)]+)")


def parse_mac_mount(text):
    """Parse macOS `mount` output into (source, mount point, fs type)."""
    out = []
    for line in text.splitlines():
        m = _MAC_MOUNT_RE.match(line)
        if m:
            out.append((m.group(1), m.group(2), m.group(3).strip()))
    return out


def read_mount_table():
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
                return parse_proc_mounts(f.read())
        if sys.platform == "darwin":
            r = subprocess.run(["/sbin/mount"], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, universal_newlines=True, timeout=15)
            return parse_mac_mount(r.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def ltfs_reason(path, table=None):
    """Return why path is on a live LTFS tape, or None."""
    rp = os.path.realpath(path)
    for mp in KNOWN_TAPE_MOUNTS:
        mpr = os.path.realpath(mp)
        if rp == mpr or rp.startswith(mpr.rstrip("/") + "/"):
            return "{0} is in the tape mount folder {1}".format(rp, mp)
    if table is None:
        table = read_mount_table()
    best = None
    for src, mp, fstype in table:
        mpn = mp.rstrip("/") or "/"
        if rp == mpn or rp.startswith(mpn.rstrip("/") + "/"):
            if best is None or len(mpn) > len(best[1]):
                best = (src, mpn, fstype)
    if best and ("ltfs" in best[0].lower() or "ltfs" in best[2].lower()):
        return "{0} is on an LTFS tape mounted at {1}".format(rp, best[1])
    if hasattr(os, "getxattr"):
        try:
            os.getxattr(rp, "user.ltfs.volumeName")
            return "{0} is on an LTFS tape".format(rp)
        except OSError:
            pass
    return None


# ---------------------------------------------------------------------------
# Inputs: a live folder or a list file
# ---------------------------------------------------------------------------

def compile_ignore(patterns):
    return [(p, re.compile(fnmatch.translate(p), re.IGNORECASE)) for p in patterns]


def junk_pattern(name, ignore):
    for pat, rx in ignore:
        if rx.match(name):
            return pat
    return None


def split_list_path(p):
    sep = "/" if ("/" in p or "\\" not in p) else "\\"
    return tuple(c for c in p.split(sep) if c not in ("", "."))


def _detect_encoding(path):
    with open(path, "rb") as f:
        head = f.read(4)
        if head.startswith((b"\xff\xfe", b"\xfe\xff")):
            return "utf-16", None
        f.seek(0)
        dec = codecs.getincrementaldecoder("utf-8")()
        try:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    dec.decode(b"", final=True)
                    break
                dec.decode(chunk)
        except UnicodeDecodeError:
            return "cp1252", "not valid UTF-8, so it was read as Windows-1252"
    return "utf-8-sig", None


def _colname(s):
    return re.sub(r"[^a-z]", "", s.lower())


class Dataset:
    """One side of a comparison: a live folder or a list file."""

    def __init__(self, path, label, kind):
        self.path = path
        self.label = label
        self.kind = kind                      # "folder" or "list"
        self.name = os.path.basename(path.rstrip("/\\")) or path
        self.fmt = "live folder" if kind == "folder" else None
        self.has_sizes = True
        self.volume = None                    # first path part of a list (tape volume name)
        self.records = []                     # (nfc parts tuple, full path as given, size or None)
        self.errors = []                      # (full path, problem)
        self.skipped = Counter()              # junk pattern -> count
        self.notes = []
        self.marc_parents = {}                # parent parts -> [marc codes]
        self.all_marc = set()
        self.chosen_marc = set()
        self.chosen_parents = []
        self.index = {}                       # key -> [(size, full path)]
        self.not_compared = []                # (reason, full path, size)
        self.scanned = False

    # -- reading ------------------------------------------------------------

    def scan(self, ignore):
        if self.scanned:
            return
        if self.kind == "folder":
            self._scan_folder(ignore)
        else:
            self._scan_list(ignore)
        self.scanned = True

    def _scan_folder(self, ignore):
        prog = Progress("Reading " + self.label, "files")
        n = 0
        stack = [(self.path, ())]
        while stack:
            dirpath, rel = stack.pop()
            try:
                with _SCANDIR(dirpath) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError as e:
                self.errors.append((dirpath, "could not open folder: {0}".format(e.strerror or e)))
                continue
            subdirs = []
            for entry in entries:
                name = entry.name
                pat = junk_pattern(name, ignore)
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False
                if is_dir:
                    if pat:
                        self.skipped[pat + " (folder; contents not listed)"] += 1
                    else:
                        subdirs.append((os.path.join(dirpath, name), rel + (nfc(name),)))
                    continue
                if pat:
                    self.skipped[pat] += 1
                    continue
                full = os.path.join(dirpath, name)
                try:
                    size = _LSTAT(full).st_size
                except OSError as e:
                    self.errors.append((full, "could not read file information: {0}".format(e.strerror or e)))
                    continue
                self.records.append((rel + (nfc(name),), full, size))
                n += 1
                prog.tick(n)
            stack.extend(reversed(subdirs))
        prog.done()

    def _scan_list(self, ignore):
        enc, enc_note = _detect_encoding(self.path)
        if enc_note:
            self.notes.append("{0}: the list is {1}.".format(self.label, enc_note))
        prog = Progress("Reading " + self.label, "lines")
        with open(self.path, encoding=enc, errors="replace", newline="") as f:
            lines = iter(f)
            first, lineno = None, 0
            for line in lines:
                lineno += 1
                if line.strip():
                    first = line.rstrip("\r\n").lstrip("﻿")
                    break
            if first is None:
                raise UsageError("{0}: the list file is empty: {1}".format(self.label, self.path))

            def add(pathstr, size, where):
                parts = split_list_path(pathstr)
                if not parts:
                    return
                if self.volume is None:
                    self.volume = parts[0]
                for c in parts:
                    pat = junk_pattern(c, ignore)
                    if pat:
                        self.skipped[pat] += 1
                        return
                self.records.append((tuple(nfc(c) for c in parts), pathstr, size))

            def parse_size(s, where):
                try:
                    return int(s.strip().replace(",", ""))
                except ValueError:
                    self.errors.append((where, "could not read the size '{0}'".format(s.strip())))
                    return False

            low = first.lower()
            tab_fields = first.split("\t")
            where_fmt = "{0} line {1}"
            if low.strip() == TAPE_INVENTORY_HEADER or (len(tab_fields) >= 2 and tab_fields[0].strip().isdigit()):
                headerless = low.strip() != TAPE_INVENTORY_HEADER
                self.fmt = "tape-inventory TSV" if not headerless else "size<TAB>path list"
                pending = [(lineno, first)] if headerless else []

                def gen():
                    for item in pending:
                        yield item
                    ln = lineno
                    for line in lines:
                        ln += 1
                        yield ln, line.rstrip("\r\n")

                for ln, line in gen():
                    if not line.strip():
                        continue
                    where = where_fmt.format(self.name, ln)
                    parts = line.split("\t", 1)
                    if len(parts) != 2:
                        self.errors.append((where, "line is not 'size<TAB>path'"))
                        continue
                    size = parse_size(parts[0], where)
                    if size is False:
                        continue
                    add(parts[1], size, where)
                    prog.tick(ln)
            else:
                delim = "\t" if "\t" in first else ","
                header = [_colname(h) for h in next(csv.reader([first], delimiter=delim))]
                path_i = next((header.index(c) for c in PATH_COLUMNS if c in header), None)
                size_i = next((header.index(c) for c in SIZE_COLUMNS if c in header), None)
                if path_i is not None and len(header) > 1:
                    self.fmt = "{0} with header".format("TSV" if delim == "\t" else "CSV")
                    self.has_sizes = size_i is not None
                    reader = csv.reader(lines, delimiter=delim)
                    for row in reader:
                        ln = lineno + reader.line_num
                        if not row or not any(x.strip() for x in row):
                            continue
                        where = where_fmt.format(self.name, ln)
                        if path_i >= len(row):
                            self.errors.append((where, "no path on this line"))
                            continue
                        size = None
                        if size_i is not None:
                            size = parse_size(row[size_i] if size_i < len(row) else "", where)
                            if size is False:
                                continue
                        add(row[path_i], size, where)
                        prog.tick(ln)
                else:
                    self.fmt = "path list (no sizes)"
                    self.has_sizes = False

                    def unquote(s):
                        if len(s) >= 2 and s[0] == s[-1] == '"':
                            return s[1:-1].replace('""', '"')
                        return s

                    add(unquote(first), None, where_fmt.format(self.name, lineno))
                    ln = lineno
                    for line in lines:
                        ln += 1
                        s = line.rstrip("\r\n")
                        if s.strip():
                            add(unquote(s), None, where_fmt.format(self.name, ln))
                            prog.tick(ln)
        prog.done()

    # -- MARC folders -------------------------------------------------------

    def find_marc_folders(self):
        cand = {}
        for parts, _full, _size in self.records:
            n = len(parts)
            for i in range(n - 1):
                c = parts[i]
                if MARC_RE.match(c):
                    pre = parts[:i + 1]
                    if not cand.get(pre):
                        cand[pre] = i + 2 < n and parts[i + 1].startswith(c + "_")
        confirmed = set(p for p, ok in cand.items() if ok)
        top = set(p for p in confirmed if not any(p[:j] in confirmed for j in range(1, len(p))))
        self.all_marc = top
        parents = defaultdict(list)
        for p in top:
            parents[p[:-1]].append(p[-1])
        self.marc_parents = {k: sorted(v) for k, v in parents.items()}

    def parent_display(self, parent):
        if self.kind == "folder":
            return os.path.join(self.path, *parent) if parent else self.path
        return "/".join(parent) if parent else "(top of the list)"

    def choose_parents(self, all_parents, within=None):
        if not self.marc_parents:
            raise UsageError(
                "No MARC folders found in {0} ({1}).\n"
                "  A MARC folder is a lowercase folder such as 'casfjazz' that holds object folders\n"
                "  such as 'casfjazz_000395'.".format(self.label, self.path))
        keys = sorted(self.marc_parents)
        if len(keys) > 1 and within:
            wanted = set(w.lower() for w in within)
            picked = [k for k in keys if any(part.lower() in wanted for part in k)]
            if not picked:
                raise UsageError("{0}: none of the places with MARC folders is inside a folder named {1}:\n  {2}"
                                 .format(self.label, " or ".join(within),
                                         "\n  ".join(self.parent_display(k) for k in keys)))
            keys = picked
        if len(keys) == 1 or all_parents:
            self.chosen_parents = keys
        else:
            print("\n{0}: MARC folders were found in more than one place:".format(self.label))
            for i, k in enumerate(keys, 1):
                print("  {0}) {1}  ({2} MARC folders)".format(i, self.parent_display(k), len(self.marc_parents[k])))
            if not UI.interactive:
                raise UsageError("{0}: MARC folders were found in more than one place. Run interactively "
                                 "to choose, name the folder to use with --within (e.g. --within Write2CAVPP0060), "
                                 "or add --all-parents to include all of them.".format(self.label))
            while True:
                ans = ask("Which should be compared? Enter numbers separated by commas, or 'all': ",
                          "--all-parents").lower()
                if ans == "all":
                    self.chosen_parents = keys
                    break
                try:
                    picks = sorted(set(int(x) for x in re.split(r"[,\s]+", ans) if x))
                    if picks and all(1 <= p <= len(keys) for p in picks):
                        self.chosen_parents = [keys[p - 1] for p in picks]
                        break
                except ValueError:
                    pass
                print("  Please enter numbers from the list, or 'all'.")
        self.chosen_marc = set(p + (m,) for p in self.chosen_parents for m in self.marc_parents[p])

    def locate(self, parts):
        """Return the index of the MARC folder in parts, -1 if it is in a MARC folder
        that was not selected, or None if it is not inside a MARC folder."""
        for i in range(len(parts) - 1):
            pre = parts[:i + 1]
            if pre in self.chosen_marc:
                return i
            if pre in self.all_marc:
                return -1
        return None

    def classify(self):
        idx = {}
        for parts, full, size in self.records:
            i = self.locate(parts)
            if i is None:
                self.not_compared.append(("not inside a MARC folder", full, size))
                continue
            if i == -1:
                self.not_compared.append(("in a MARC folder in a location that was not selected", full, size))
                continue
            key = "/".join(parts[i:])
            idx.setdefault(key, []).append((size, full))
        self.index = idx

    def marc_summary(self):
        out = []
        for p in self.chosen_parents:
            out.append("{0} MARC folder(s) in {1}".format(len(self.marc_parents[p]), self.parent_display(p)))
        return out

    @property
    def compared_files(self):
        return sum(len(v) for v in self.index.values())

    @property
    def md5_files(self):
        return sum(len(v) for k, v in self.index.items() if k.lower().endswith(".md5"))


def make_dataset(path, label, want=None):
    path = clean_path(path)
    if not path:
        raise UsageError("No path given for {0}.".format(label))
    if not os.path.exists(path):
        raise UsageError("{0}: can't find {1}".format(label, path))
    path = os.path.abspath(path)
    if os.path.isdir(path):
        if want == "file":
            raise UsageError("{0} must be a list file made by tape-inventory.py, not a folder: {1}\n"
                             "  This tool never reads a tape directly.".format(label, path))
        reason = ltfs_reason(path)
        if reason:
            raise UsageError("{0}: refusing to read a live tape ({1}).\n"
                             "  Make a list with tape-inventory.py and give that instead.".format(label, reason))
        return Dataset(path, label, "folder")
    return Dataset(path, label, "list")


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------

class Comparison:
    def __init__(self, name, origins, dest, missing_label=None, extra_label=None):
        self.name = name
        self.origins = origins
        self.dest = dest
        self.labels = dict(CAT_LABEL)
        if missing_label:
            self.labels["missing_from_destination"] = missing_label
        if extra_label:
            self.labels["extra_in_destination"] = extra_label
        self.exp_label = " + ".join(d.label for d in origins)
        self.dest_label = dest.label
        self.rows = defaultdict(list)
        self.counts = Counter()
        self.agg = defaultdict(Counter)
        self.notes = []
        self.excluded_md5 = False
        self.size_not_checked = 0


def _case_groups(d):
    groups = defaultdict(list)
    for k in d:
        groups[k.casefold()].append(k)
    return {cf: ks for cf, ks in groups.items() if len(ks) > 1}


def _row(cmp, cat, key, exp=None, dst=None, note=""):
    parts = key.split("/")
    es = exp[1] if exp else None
    ds = dst[1] if dst else None
    return {
        "comparison": cmp.name,
        "category": cmp.labels[cat],
        "marc_code": parts[0],
        "object_identifier": parts[1] if len(parts) > 2 else "",
        "relative_path": key,
        "expected_side": exp[0] if exp else "",
        "expected_size": "" if es is None else es,
        "destination_side": dst[0] if dst else "",
        "destination_size": "" if ds is None else ds,
        "size_difference": (ds - es) if (es is not None and ds is not None) else "",
        "expected_full_path": exp[2] if exp else "",
        "destination_full_path": dst[2] if dst else "",
        "note": note,
    }


def _sizes_text(occs):
    return ", ".join("{0} ({1})".format(o[0], "size unknown" if o[1] is None else "{0:,} bytes".format(o[1]))
                     for o in occs)


def compare(cmp):
    """Fill cmp.rows / counts / agg. Every key gets exactly one category."""
    old_lists = [d.label for d in cmp.origins + [cmp.dest]
                 if d.fmt == "tape-inventory TSV" and d.md5_files == 0 and d.compared_files > 0]
    if old_lists:
        cmp.excluded_md5 = True
        cmp.notes.append("{0} {1} no .md5 files (made before tape-inventory.py listed them), so .md5 files "
                         "were left out of this comparison.".format(
                             ", ".join(old_lists[:-1]) + " and " + old_lists[-1] if len(old_lists) > 1 else old_lists[0],
                             "have" if len(old_lists) > 1 else "has"))

    def build(datasets):
        d = {}
        for ds in datasets:
            for key, occ in ds.index.items():
                if cmp.excluded_md5 and key.lower().endswith(".md5"):
                    continue
                lst = d.get(key)
                if lst is None:
                    d[key] = lst = []
                lst.extend((ds.label, s, p) for s, p in occ)
        return d

    exp = build(cmp.origins)
    dst = build([cmp.dest])
    status = {}
    R = cmp.rows

    # 1. Duplicates: the same path more than once on either side.
    for key in set(exp) | set(dst):
        e, a = exp.get(key, []), dst.get(key, [])
        if len(e) > 1 or len(a) > 1:
            status[key] = "duplicate_across_sources"
            note = "expected side: {0}; destination: {1}".format(
                _sizes_text(e) or "not present", _sizes_text(a) or "not present")
            for o in e:
                R["duplicate_across_sources"].append(_row(cmp, "duplicate_across_sources", key, exp=o, note=note))
            for o in a:
                R["duplicate_across_sources"].append(_row(cmp, "duplicate_across_sources", key, dst=o, note=note))

    # 2. Names that differ only in case within one side.
    for side_name, side, label in (("expected side", exp, cmp.exp_label), ("destination", dst, cmp.dest_label)):
        for cf, keys in _case_groups(side).items():
            for key in keys:
                if key in status and status[key] != "case_conflict":
                    continue
                status[key] = "case_conflict"
                others = [k for k in keys if k != key]
                e = exp.get(key, [None])[0]
                a = dst.get(key, [None])[0]
                R["case_conflict"].append(_row(
                    cmp, "case_conflict", key, exp=e, dst=a,
                    note="the {0} ({1}) also has {2}".format(side_name, label, "; ".join(others))))

    # 3. On both sides: compare size.
    for key in exp:
        if key in status or key not in dst:
            continue
        e, a = exp[key][0], dst[key][0]
        if e[1] is None or a[1] is None:
            status[key] = "matched"
            cmp.size_not_checked += 1
            R["matched"].append(_row(cmp, "matched", key, exp=e, dst=a, note="size not checked (no size in a list)"))
        elif e[1] == a[1]:
            status[key] = "matched"
            R["matched"].append(_row(cmp, "matched", key, exp=e, dst=a))
        else:
            status[key] = "size_mismatch"
            R["size_mismatch"].append(_row(cmp, "size_mismatch", key, exp=e, dst=a))

    # 4. One side only. Pair up names that differ only in case.
    by_cf = defaultdict(lambda: ([], []))
    for key in exp:
        if key not in status and key not in dst:
            by_cf[key.casefold()][0].append(key)
    for key in dst:
        if key not in status and key not in exp:
            by_cf[key.casefold()][1].append(key)
    for cf, (ms, xs) in by_cf.items():
        if ms and xs:
            for key in ms:
                status[key] = "case_conflict"
                R["case_conflict"].append(_row(cmp, "case_conflict", key, exp=exp[key][0],
                                               note="destination has {0}".format("; ".join(xs))))
            for key in xs:
                status[key] = "case_conflict"
                R["case_conflict"].append(_row(cmp, "case_conflict", key, dst=dst[key][0],
                                               note="expected side has {0}".format("; ".join(ms))))
        else:
            for key in ms:
                status[key] = "missing_from_destination"
                R["missing_from_destination"].append(_row(cmp, "missing_from_destination", key, exp=exp[key][0]))
            for key in xs:
                status[key] = "extra_in_destination"
                R["extra_in_destination"].append(_row(cmp, "extra_in_destination", key, dst=dst[key][0]))

    # Counts and summary totals.
    cmp.counts = Counter(status.values())
    for key, st in status.items():
        parts = key.split("/")
        marc = parts[0]
        obj = parts[1] if len(parts) > 2 else ""
        for scope in (("TOTAL", "", ""), ("MARC", marc, ""), ("OBJECT", marc, obj)):
            c = cmp.agg[scope]
            c[st] += 1
            for side, prefix in ((exp, "exp"), (dst, "dst")):
                for o in side.get(key, ()):
                    c[prefix + "_files"] += 1
                    if o[1] is None:
                        c[prefix + "_unknown"] += 1
                    else:
                        c[prefix + "_bytes"] += o[1]
    for cat in R:
        R[cat].sort(key=lambda r: (r["relative_path"], r["expected_side"], r["destination_side"]))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _open_csv(path):
    return open(path, "w", encoding="utf-8-sig", errors="backslashreplace", newline="")


def write_reports(folder, run):
    with _open_csv(os.path.join(folder, "summary.csv")) as f:
        w = csv.DictWriter(f, SUMMARY_COLUMNS)
        w.writeheader()
        for cmp in run["comparisons"]:
            scopes = sorted(cmp.agg, key=lambda s: ({"TOTAL": 0}.get(s[0], 1), s[1],
                                                    0 if s[0] == "MARC" else 1, s[2]))
            for scope in scopes:
                c = cmp.agg[scope]
                level, marc, obj = scope
                if level == "OBJECT" and obj == "":
                    obj = "(files directly in the MARC folder)"
                problems = sum(c[k] for k in DISCREPANCY_CATS)

                def b(prefix):
                    return "" if c[prefix + "_unknown"] else c[prefix + "_bytes"]

                def g(prefix):
                    return "" if c[prefix + "_unknown"] else round(gb(c[prefix + "_bytes"]), 3)

                w.writerow({
                    "comparison": cmp.name, "level": level, "marc_code": marc, "object_identifier": obj,
                    "expected_side": cmp.exp_label, "expected_files": c["exp_files"],
                    "expected_bytes": b("exp"), "expected_GB": g("exp"),
                    "destination_side": cmp.dest_label, "destination_files": c["dst_files"],
                    "destination_bytes": b("dst"), "destination_GB": g("dst"),
                    "matched": c["matched"],
                    "missing_from_destination": c["missing_from_destination"],
                    "extra_in_destination": c["extra_in_destination"],
                    "size_mismatch": c["size_mismatch"],
                    "duplicate_across_sources": c["duplicate_across_sources"],
                    "case_conflict": c["case_conflict"],
                    "status": "OK" if problems == 0 else "CHECK",
                })

    for cat in COMPARE_CATS:
        with _open_csv(os.path.join(folder, cat + ".csv")) as f:
            w = csv.DictWriter(f, DETAIL_COLUMNS)
            w.writeheader()
            for cmp in run["comparisons"]:
                w.writerows(cmp.rows.get(cat, []))

    with _open_csv(os.path.join(folder, "unreadable.csv")) as f:
        w = csv.writer(f)
        w.writerow(UNREADABLE_COLUMNS)
        for ds in run["datasets"]:
            for full, problem in ds.errors:
                w.writerow([ds.label, full, problem])

    with _open_csv(os.path.join(folder, "not_compared.csv")) as f:
        w = csv.writer(f)
        w.writerow(NOT_COMPARED_COLUMNS)
        for ds in run["datasets"]:
            for reason, full, size in ds.not_compared:
                w.writerow([ds.label, reason, full, "" if size is None else size])

    with open(os.path.join(folder, "run_info.txt"), "w", encoding="utf-8", errors="backslashreplace") as f:
        f.write(run["info_text"])


def _num(v):
    if isinstance(v, str) and v:
        if re.fullmatch(r"-?\d+", v):
            return int(v)
        if re.fullmatch(r"-?\d+\.\d+", v):
            return float(v)
    return v


def write_xlsx(folder, base):
    try:
        import openpyxl
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Font
    except ImportError:
        return "openpyxl is not installed, so no .xlsx was made. Install it with: pip install openpyxl"
    path = os.path.join(folder, base + ".xlsx")
    wb = openpyxl.Workbook(write_only=True)
    bold = Font(bold=True)
    sheets = [("Summary", "summary.csv")] + [(CAT_LABEL[c][:31], c + ".csv") for c in COMPARE_CATS] + \
             [("Unreadable", "unreadable.csv"), ("Not compared", "not_compared.csv")]
    limit = 1048575
    for title, fname in sheets:
        ws = wb.create_sheet(title)
        ws.freeze_panes = "A2"
        with open(os.path.join(folder, fname), encoding="utf-8-sig", newline="") as f:
            for n, row in enumerate(csv.reader(f)):
                if n == 0:
                    cells = []
                    for v in row:
                        c = WriteOnlyCell(ws, value=v)
                        c.font = bold
                        cells.append(c)
                    ws.append(cells)
                    continue
                if n >= limit:
                    ws.append(["(more rows in {0})".format(fname)])
                    break
                ws.append([_num(v) for v in row])
    ws = wb.create_sheet("Run info")
    with open(os.path.join(folder, "run_info.txt"), encoding="utf-8") as f:
        for line in f:
            ws.append([line.rstrip("\n")])
    wb.save(path)
    return None


def write_partner_inventory(ds, any_problems, assume_yes):
    """The one write outside the report folder: an inventory CSV at the partner drive root."""
    if ds.kind != "folder":
        return "Partner inventory not written: the partner drive was given as a list, not a live drive."
    fname = "{0}_Inventory_{1}.csv".format(safe_name(ds.name), datetime.date.today().isoformat())
    target = os.path.join(ds.path, fname)
    if os.path.exists(target):
        return "Partner inventory not written: {0} already exists (it was not changed).".format(target)
    if not os.access(ds.path, os.W_OK):
        return "Partner inventory not written: the partner drive can't be written to ({0}).".format(ds.path)
    q = ("Discrepancies were found. " if any_problems else "") + \
        "Write {0} to the root of the partner drive ({1})? (yes/no): ".format(fname, ds.path)
    if not (assume_yes or ask_yes(q, "--yes")):
        return "Partner inventory not written (you said no)."
    sidecars = {}
    for parts, full, size in ds.records:
        if full[-4:].lower() == ".md5":
            sidecars[full[:-4]] = full
    with _open_csv(target) as f:
        w = csv.writer(f)
        w.writerow(INVENTORY_COLUMNS)
        for parts, full, size in sorted(ds.records, key=lambda r: r[0]):
            i = ds.locate(parts)
            marc = parts[i] if i is not None and i >= 0 else ""
            obj = parts[i + 1] if i is not None and i >= 0 and i + 2 < len(parts) else ""
            md5 = read_md5_sidecar(sidecars[full]) if full in sidecars else ""
            w.writerow([marc, obj, "/".join(parts), parts[-1], size, md5 or ""])
    return "Partner inventory written: {0}".format(target)


def read_md5_sidecar(path):
    """Read the hash from an md5tool.py sidecar ('hash *filename'). None if not found."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(65536)
    except OSError:
        return None
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith(";"):
            continue
        token = re.split(r"[\s*]", s, maxsplit=1)[0].lower()
        if re.fullmatch(r"[0-9a-f]{32}", token):
            return token
    return None


# ---------------------------------------------------------------------------
# Screen summary
# ---------------------------------------------------------------------------

def build_summary_text(run, exit_code, report_dir):
    L = []
    bar = "=" * 72
    L.append(bar)
    L.append("{0} {1}  |  {2} check  |  {3}".format(TOOL, __version__, run["job"], run["label"]))
    L.append("Run {0}".format(run["started"]))
    L.append(bar)
    L.append("Inputs")
    for ds in run["datasets"]:
        L.append("  {0}: {1}".format(ds.label, ds.path))
        extra = ", volume {0}".format(ds.volume) if ds.volume and ds.kind == "list" else ""
        L.append("      {0}{1}".format(ds.fmt, extra))
        L.append("      {0:,} files compared, {1:,} junk skipped, {2:,} not compared, {3:,} unreadable".format(
            ds.compared_files, sum(ds.skipped.values()), len(ds.not_compared), len(ds.errors)))
        for line in ds.marc_summary():
            L.append("      " + line)
        if ds.skipped:
            L.append("      skipped: " + ", ".join("{0} x{1:,}".format(k, v) for k, v in sorted(ds.skipped.items())))
    for n, cmp in enumerate(run["comparisons"], 1):
        t = cmp.agg.get(("TOTAL", "", ""), Counter())
        L.append("-" * 72)
        L.append("{0}. {1}".format(n, cmp.name))

        def side(prefix, label):
            size = "size unknown" if t[prefix + "_unknown"] else fmt_gb(t[prefix + "_bytes"])
            return "{0:>10,} files  {1:>16}  {2}".format(t[prefix + "_files"], size, label)

        L.append("   expected    " + side("exp", cmp.exp_label))
        L.append("   destination " + side("dst", cmp.dest_label))
        for cat in COMPARE_CATS:
            L.append("   {0:<34}{1:>10,}".format(cmp.labels[cat], cmp.counts[cat]))
        problems = sum(cmp.counts[c] for c in DISCREPANCY_CATS)
        L.append("   -> {0}".format("OK" if problems == 0 else "CHECK: {0:,} discrepancies".format(problems)))
        if cmp.size_not_checked:
            L.append("   note: size not checked for {0:,} files (a list has no sizes)".format(cmp.size_not_checked))
        for note in cmp.notes:
            L.append("   note: " + note)
    notes = list(run["notes"])
    for ds in run["datasets"]:
        notes.extend(ds.notes)
    if notes:
        L.append("-" * 72)
        L.append("Notes")
        for note in notes:
            L.append("  - " + note)
    L.append(bar)
    L.append("RESULT: {0} (exit code {1})".format(EXIT_MEANING[exit_code], exit_code))
    if report_dir:
        L.append("Reports: {0}".format(report_dir))
    L.append(bar)
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def gather_interim(args):
    sources = [s for s in (args.source or []) if clean_path(s)]
    if not sources:
        sources.append(ask_path("Drag in source 1 (a drive, a folder, or a list file), then press Enter: ",
                                "--source"))
        while True:
            p = ask_path("Drag in another source, or just press Enter if there are no more: ",
                         "--source", allow_blank=True)
            if not p:
                break
            sources.append(p)
    dest = args.dest or ask_path("Drag in the interim drive or folder (or its list file), then press Enter: ",
                                 "--dest")
    many = len(sources) > 1
    origins = [make_dataset(p, "source {0}".format(i) if many else "source") for i, p in enumerate(sources, 1)]
    dest_ds = make_dataset(dest, "interim drive")
    comps = [Comparison("sources -> interim drive", origins, dest_ds)]
    return origins + [dest_ds], comps, dest_ds.volume or dest_ds.name


def gather_partner(args):
    src = args.source_list or ask_path(
        "Drag in the source inventory (stands in for the MediaPreserve drive list for now): ", "--source-list")
    vendor = args.vendor or ask_path("Drag in the vendor drive (or its list file): ", "--vendor")
    partner = args.partner or ask_path("Drag in the partner drive (or its list file): ", "--partner")
    s = make_dataset(src, "source inventory")
    v = make_dataset(vendor, "vendor drive")
    p = make_dataset(partner, "partner drive")
    comps = [Comparison("source inventory -> vendor drive", [s], v),
             Comparison("vendor drive -> partner drive", [v], p)]
    return [s, v, p], comps, p.name


def gather_tape(args):
    src = args.source or ask_path("Drag in the interim drive inventory (the source for this write): ", "--source")

    def pre(arg, first, tape):
        if arg:
            return arg
        if first:
            return None
        p = ask_path("Drag in tape {0}'s PRE-write inventory, or press Enter if this is the first "
                     "write to tape {0}: ".format(tape), "--{0}-pre (or --{0}-first-write)".format(tape.lower()),
                     allow_blank=True, want="file")
        return p

    a_pre = pre(args.a_pre, args.a_first_write, "A")
    a_post = args.a_post or ask_path("Drag in tape A's POST-write inventory: ", "--a-post", want="file")
    b_pre = pre(args.b_pre, args.b_first_write, "B")
    b_post = args.b_post or ask_path("Drag in tape B's POST-write inventory: ", "--b-post", want="file")

    source = make_dataset(src, "source")
    A_pre = make_dataset(a_pre, "A pre-write", want="file") if a_pre else None
    A_post = make_dataset(a_post, "A post-write", want="file")
    B_pre = make_dataset(b_pre, "B pre-write", want="file") if b_pre else None
    B_post = make_dataset(b_post, "B post-write", want="file")

    comps = [
        Comparison("A: {0} -> A post-write".format("pre-write + source" if A_pre else "source (first write)"),
                   ([A_pre] if A_pre else []) + [source], A_post),
        Comparison("B: {0} -> B post-write".format("pre-write + source" if B_pre else "source (first write)"),
                   ([B_pre] if B_pre else []) + [source], B_post),
        Comparison("A post-write <-> B post-write", [A_post], B_post,
                   missing_label="Only on A post-write", extra_label="Only on B post-write"),
    ]
    datasets = [d for d in (source, A_pre, A_post, B_pre, B_post) if d]
    return datasets, comps, None


def tape_checks(datasets, run):
    by = {d.label: d for d in datasets}
    a, b = by["A post-write"].volume, by["B post-write"].volume
    for tape in ("A", "B"):
        pre, post = by.get(tape + " pre-write"), by[tape + " post-write"]
        if pre and pre.volume != post.volume:
            run["notes"].append("WARNING: tape {0} pre-write volume ({1}) is not the post-write volume ({2}). "
                                "Check that these lists are for the same tape.".format(tape, pre.volume, post.volume))
        if post.volume and not post.volume.upper().endswith(tape):
            run["notes"].append("WARNING: the tape {0} post-write volume is {1}, which does not end in '{0}'."
                                .format(tape, post.volume))
    if a and a == b:
        run["notes"].append("WARNING: the A and B post-write lists have the same volume name ({0}).".format(a))
    label = os.path.commonprefix([a or "", b or ""]).rstrip("_-") or "tape"
    return label


def choose_output(args, live_roots):
    cwd = os.getcwd()
    asked = not args.out
    while True:
        if args.out:
            out = clean_path(args.out)
        else:
            raw = ask("Where should the reports be saved? Drag in a folder, or press Enter for the "
                      "current folder\n  ({0}): ".format(cwd), "--out")
            out = clean_path(raw) or cwd
        out = os.path.abspath(out)
        problem = None
        if os.path.exists(out) and not os.path.isdir(out):
            problem = "That is a file, not a folder."
        elif ltfs_reason(out):
            problem = "That is on a live tape. Reports can't be saved to tape."
        else:
            rout = os.path.realpath(out)
            for root in live_roots:
                rr = os.path.realpath(root)
                if rout == rr or rout.startswith(rr.rstrip(os.sep) + os.sep):
                    problem = "That is inside {0}, which is being compared. Pick a folder outside it.".format(root)
                    break
        if problem is None and not os.path.exists(out):
            if asked and not ask_yes("  {0} does not exist. Create it? (yes/no): ".format(out), "--out"):
                problem = "Folder not created."
            else:
                try:
                    os.makedirs(out)
                except OSError as e:
                    problem = "Could not create it: {0}".format(e)
        if problem is None and not os.access(out, os.W_OK):
            problem = "Can't write to that folder."
        if problem is None:
            return out
        if not asked:
            raise UsageError("--out {0}: {1}".format(out, problem))
        print("  " + problem)


def run(args):
    global LAST_REPORT_DIR
    LAST_REPORT_DIR = None
    started = datetime.datetime.now()
    patterns = ([] if args.no_default_ignore else list(DEFAULT_IGNORE)) + list(args.ignore or [])
    ignore = compile_ignore(patterns)

    job = args.job
    if job is None:
        if not UI.interactive:
            raise UsageError("Choose a job: interim, partner or tape (see --help).")
        print("What would you like to check?")
        print("  1) interim  - DG backup source drives/folders vs the Write2LTO interim drive")
        print("  2) partner  - AV source inventory vs vendor drive vs partner drive")
        print("  3) tape     - interim drive inventory vs tape A and tape B inventories")
        choice = ask("Enter 1, 2 or 3: ", "a job")
        job = {"1": "interim", "2": "partner", "3": "tape"}.get(choice)
        if not job:
            raise UsageError("Please choose 1, 2 or 3.")
        for name in ("source", "dest", "source_list", "vendor", "partner", "a_pre", "a_post", "b_pre", "b_post",
                     "write_inventory", "yes", "a_first_write", "b_first_write"):
            if not hasattr(args, name):
                setattr(args, name, None)

    if job == "interim":
        datasets, comps, label = gather_interim(args)
    elif job == "partner":
        datasets, comps, label = gather_partner(args)
    else:
        datasets, comps, label = gather_tape(args)

    live_roots = [d.path for d in datasets if d.kind == "folder"]
    out_root = choose_output(args, live_roots)

    run_info = {"job": job, "label": "", "started": started.strftime("%Y-%m-%d %H:%M:%S"),
                "datasets": datasets, "comparisons": comps, "notes": []}

    print("\nReading inputs (names and sizes only)...")
    for ds in datasets:
        ds.scan(ignore)
        print("  {0}: {1:,} files listed ({2})".format(ds.label, len(ds.records), ds.fmt))
    for ds in datasets:
        ds.find_marc_folders()
        ds.choose_parents(args.all_parents, args.within)
        ds.classify()

    if job == "tape":
        label = tape_checks(datasets, run_info)
    label = args.label or label or job
    run_info["label"] = label

    for cmp in comps:
        compare(cmp)

    if any(d.errors for d in datasets):
        exit_code = EXIT_READ_ERROR
    elif any(sum(c.counts[k] for k in DISCREPANCY_CATS) for c in comps):
        exit_code = EXIT_DISCREPANCY
    else:
        exit_code = EXIT_OK

    stamp = started.strftime("%Y-%m-%d_%H%M%S")
    base = "car_pvr_verify_{0}_{1}_{2}".format(job, safe_name(label), stamp)
    report_dir = os.path.join(out_root, base)
    n = 1
    while os.path.exists(report_dir):
        n += 1
        report_dir = os.path.join(out_root, "{0}_{1}".format(base, n))
    os.makedirs(report_dir)
    LAST_REPORT_DIR = report_dir

    info = ["{0} {1}".format(TOOL, __version__),
            "Command: " + " ".join(sys.argv),
            "Ignore patterns: " + ", ".join(patterns),
            ""]
    summary = build_summary_text(run_info, exit_code, report_dir)
    run_info["info_text"] = "\n".join(info) + summary
    write_reports(report_dir, run_info)

    if args.xlsx:
        problem = write_xlsx(report_dir, base)
        if problem:
            run_info["notes"].append(problem)

    if job == "partner" and getattr(args, "write_inventory", False):
        partner = [d for d in datasets if d.label == "partner drive"][0]
        msg = write_partner_inventory(partner, exit_code != EXIT_OK, getattr(args, "yes", False))
        run_info["notes"].append(msg)

    summary = build_summary_text(run_info, exit_code, report_dir)
    run_info["info_text"] = "\n".join(info) + summary
    with open(os.path.join(report_dir, "run_info.txt"), "w", encoding="utf-8", errors="backslashreplace") as f:
        f.write(run_info["info_text"])
    print()
    print(summary, end="")
    return exit_code


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

EPILOG = """
jobs:
  interim   --source PATH [--source PATH ...] --dest PATH
  partner   --source-list PATH --vendor PATH --partner PATH [--write-inventory]
  tape      --source PATH --a-pre LIST --a-post LIST --b-pre LIST --b-post LIST
            (use --a-first-write / --b-first-write when a tape had nothing on it before)

Each PATH can be a live folder or drive, or a list file: a tape-inventory.py
TSV, a 'size<TAB>path' list, a CSV/TSV with a path column, or a plain list of
paths. Tape inputs must be tape-inventory.py lists: this tool never reads a tape.
Anything you leave out, the tool asks for.

exit codes:
  0    everything matched
  1    discrepancies found (missing, extra, size, duplicate, case)
  2    input or usage error
  3    read errors: something could not be read, so results are incomplete
  130  interrupted (Ctrl-C)
"""


def build_parser():
    p = argparse.ArgumentParser(
        prog=TOOL, formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
        description="Check that CA-R files were copied completely and correctly at PVR "
                    "(partner drives, the LTO interim drive, and LTO tapes). Read-only.")
    p.add_argument("--selftest", action="store_true", help="build temporary test files and check every report category")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", metavar="FOLDER", help="where to save the reports (otherwise you are asked)")
    common.add_argument("--xlsx", action="store_true", help="also save an .xlsx workbook (needs openpyxl)")
    common.add_argument("--ignore", action="append", metavar="PATTERN",
                        help="another junk file or folder name to skip, e.g. 'Thumbs.db' or '*.tmp' (repeatable)")
    common.add_argument("--no-default-ignore", action="store_true", help="do not skip the default junk names")
    common.add_argument("--all-parents", action="store_true",
                        help="if MARC folders are found in more than one place, compare all of them without asking")
    common.add_argument("--within", action="append", metavar="FOLDER",
                        help="if MARC folders are found in more than one place, use the ones inside a folder "
                             "with this name, e.g. Write2CAVPP0060 (repeatable)")
    common.add_argument("--label", help="name used in the report folder (default: worked out from the inputs)")

    sub = p.add_subparsers(dest="job", metavar="{interim,partner,tape}")
    si = sub.add_parser("interim", parents=[common], help="DG: backup sources vs the Write2LTO interim drive")
    si.add_argument("--source", action="append", metavar="PATH", help="a backup source drive, folder or list (repeatable)")
    si.add_argument("--dest", metavar="PATH", help="the interim drive or folder, or its list")

    sp = sub.add_parser("partner", parents=[common], help="AV: source inventory vs vendor drive vs partner drive")
    sp.add_argument("--source-list", metavar="PATH", help="source inventory (stands in for the MediaPreserve list)")
    sp.add_argument("--vendor", metavar="PATH", help="the vendor drive, or its list")
    sp.add_argument("--partner", metavar="PATH", help="the partner drive, or its list")
    sp.add_argument("--write-inventory", action="store_true",
                    help="after checking, write an inventory CSV to the root of the partner drive (asks first)")
    sp.add_argument("--yes", action="store_true", help="answer yes to the partner inventory question")

    st = sub.add_parser("tape", parents=[common], help="LTO: interim inventory vs tape A and B inventories")
    st.add_argument("--source", metavar="PATH", help="the interim drive inventory (or the interim folder)")
    st.add_argument("--a-pre", metavar="LIST", help="tape A pre-write inventory")
    st.add_argument("--a-post", metavar="LIST", help="tape A post-write inventory")
    st.add_argument("--b-pre", metavar="LIST", help="tape B pre-write inventory")
    st.add_argument("--b-post", metavar="LIST", help="tape B post-write inventory")
    st.add_argument("--a-first-write", action="store_true", help="tape A had nothing on it before this write")
    st.add_argument("--b-first-write", action="store_true", help="tape B had nothing on it before this write")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    for name in ("out", "xlsx", "ignore", "no_default_ignore", "all_parents", "within", "label"):
        if not hasattr(args, name):
            setattr(args, name, None)
    try:
        return run(args)
    except UsageError as e:
        print("\nError: {0}".format(e), file=sys.stderr)
        return EXIT_USAGE


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def selftest():
    global _SCANDIR, _LSTAT
    print("{0} {1} self-test".format(TOOL, __version__))
    results = []

    def check(name, cond, detail=""):
        results.append(bool(cond))
        print("  {0}  {1}{2}".format("PASS" if cond else "FAIL", name,
                                     "" if cond or not detail else "   [{0}]".format(detail)))

    saved_interactive = UI.interactive
    saved_mounts = list(KNOWN_TAPE_MOUNTS)
    UI.interactive = False
    tmp = tempfile.mkdtemp(prefix="car_pvr_selftest_")
    try:
        T = lambda *p: os.path.join(tmp, *p)
        out = T("reports")
        os.makedirs(out)

        def mk(path, size=10, data=None):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data if data is not None else b"x" * size)

        def tsv(path, rows, header=True):
            with open(path, "w", encoding="utf-8", newline="") as f:
                if header:
                    f.write("SizeinBytes\tFullFilePath\n")
                for size, p in rows:
                    f.write("{0}\t{1}\n".format(size, p))

        def go(argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = main(argv if "--out" in argv else argv + ["--out", out])
            return code, LAST_REPORT_DIR, buf.getvalue()

        def rows(folder, cat):
            with open(os.path.join(folder, cat + ".csv"), encoding="utf-8-sig", newline="") as f:
                return list(csv.DictReader(f))

        def keys(folder, cat, comparison=None):
            return sorted(set(r["relative_path"] for r in rows(folder, cat)
                              if comparison is None or r["comparison"].startswith(comparison)))

        def snapshot(*roots):
            snap = {}
            for root in roots:
                for dp, dn, fn in os.walk(root):
                    for n in fn:
                        p = os.path.join(dp, n)
                        st = os.lstat(p)
                        snap[p] = (st.st_size, st.st_mtime_ns)
            return snap

        # -- path cleaning and tape guards ---------------------------------
        print("Inputs and safety")
        check("drag-and-drop quotes stripped", clean_path("'/tmp/a b' ") == "/tmp/a b")
        check("double quotes stripped", clean_path('"/tmp/x y"') == "/tmp/x y")
        if os.name != "nt":
            check("backslash escapes removed", clean_path("/tmp/a\\ b\\(1\\)  ") == "/tmp/a b(1)")
        linux = parse_proc_mounts("ltfs /mnt/tape\\040one fuse rw,nosuid 0 0\n/dev/sda1 /mnt/data ext4 rw 0 0\n")
        check("Linux LTFS mount refused", ltfs_reason("/mnt/tape one/caevhs", linux) is not None)
        check("Linux ordinary drive allowed", ltfs_reason("/mnt/data/x", linux) is None)
        mac = parse_mac_mount("ltfs on /Volumes/CAVPP0001L8A (ltfs, local, nodev)\n"
                              "/dev/disk4s2 on /Volumes/CAR_043 (exfat, local, nodev)\n")
        check("macOS LTFS mount refused", ltfs_reason("/Volumes/CAVPP0001L8A/c", mac) is not None)
        check("macOS ordinary drive allowed", ltfs_reason("/Volumes/CAR_043/x", mac) is None)

        # -- job A: interim -------------------------------------------------
        print("interim job")
        S1 = T("SRC1")
        W = os.path.join(S1, "Write2TEST")
        mk(os.path.join(W, "cabc/cabc_000001/cabc_000001_prsv.tif"), 100)
        mk(os.path.join(W, "cabc/cabc_000001/cabc_000001_prsv.tif.md5"),
           data=b"16fbc96a1342367ebc22981af26d303d *cabc_000001_prsv.tif\n")
        mk(os.path.join(W, "cabc/cabc_000001/cabc_000001_access.jpg"), 50)
        mk(os.path.join(W, "cabc/cabc_000001/cabc_000001_access.pkf"), 5)
        mk(os.path.join(W, "cabc/cabc_000001/.DS_Store"), 6)
        mk(os.path.join(W, "cabc/cabc_00002/cabc_00002_prsv.tif"), 70)
        mk(os.path.join(W, "cabc/qualified_dublinCore_export_log_cabc.txt"), 20)
        mk(os.path.join(W, "cabc/cabc_000003/cabc_000003_gone.tif"), 30)
        mk(os.path.join(W, "cabc/cabc_000003/cabc_000003_resized.tif"), 40)
        mk(os.path.join(W, "cabc/cabc_000004/cabc_000004_Upper.tif"), 12)
        nfd = unicodedata.normalize("NFD", "cabc_000005_café.tif")
        mk(os.path.join(W, "cabc/cabc_000005", nfd), 15)
        mk(os.path.join(W, "cdup/cdup_000001/cdup_000001_prsv.tif"), 25)
        mk(os.path.join(W, "cabc/cabc_000006/cabc_000006_locked.tif"), 5)
        mk(os.path.join(W, "cabc/cabc_000007/cabc_000007_prsv.tif"), 5)
        mk(os.path.join(S1, ".DS_Store"), 6)
        mk(os.path.join(S1, "._Write2TEST"), 4)
        mk(os.path.join(S1, ".Trashes/501/old.tif"), 4)
        mk(os.path.join(S1, "2024-2025_raw/cleanup/notes.txt"), 4)
        src2 = T("src2.tsv")
        tsv(src2, [(25, "SRC2/Write2TEST/cdup/cdup_000001/cdup_000001_prsv.tif"),
                   (33, "SRC2/Write2TEST/cxyz/cxyz_000001/cxyz_000001_prsv.tif"),
                   (60, "SRC2/Write2TEST/cxyz/cxyz_000001/cxyz_000001_prsv.tif.md5")])
        D = T("DEST")
        WD = os.path.join(D, "Write2TEST")
        mk(os.path.join(WD, "cabc/cabc_000001/cabc_000001_prsv.tif"), 100)
        mk(os.path.join(WD, "cabc/cabc_000001/cabc_000001_prsv.tif.md5"),
           data=b"16fbc96a1342367ebc22981af26d303d *cabc_000001_prsv.tif\n")
        mk(os.path.join(WD, "cabc/cabc_000001/cabc_000001_access.jpg"), 50)
        mk(os.path.join(WD, "cabc/cabc_000001/cabc_000001_extra.tif"), 9)
        mk(os.path.join(WD, "cabc/cabc_00002/cabc_00002_prsv.tif"), 70)
        mk(os.path.join(WD, "cabc/qualified_dublinCore_export_log_cabc.txt"), 20)
        mk(os.path.join(WD, "cabc/cabc_000003/cabc_000003_resized.tif"), 41)
        mk(os.path.join(WD, "cabc/cabc_000004/cabc_000004_upper.tif"), 12)
        mk(os.path.join(WD, "cabc/cabc_000005", "cabc_000005_café.tif"), 15)
        mk(os.path.join(WD, "cdup/cdup_000001/cdup_000001_prsv.tif"), 25)
        mk(os.path.join(WD, "cxyz/cxyz_000001/cxyz_000001_prsv.tif"), 33)
        mk(os.path.join(WD, "cxyz/cxyz_000001/cxyz_000001_prsv.tif.md5"), 60)

        before = snapshot(S1, D)
        fault_files = {os.path.join(W, "cabc/cabc_000006/cabc_000006_locked.tif")}
        fault_dirs = {os.path.join(W, "cabc/cabc_000007")}
        real_scandir, real_lstat = os.scandir, os.lstat

        def fake_scandir(p="."):
            if os.fspath(p) in fault_dirs:
                raise PermissionError(13, "Permission denied (simulated)", os.fspath(p))
            return real_scandir(p)

        def fake_lstat(p, *a, **k):
            if os.fspath(p) in fault_files:
                raise PermissionError(13, "Permission denied (simulated)", os.fspath(p))
            return real_lstat(p, *a, **k)

        _SCANDIR, _LSTAT = fake_scandir, fake_lstat
        try:
            code, rep, text = go(["interim", "--source", S1, "--source", src2, "--dest", D, "--xlsx"])
        finally:
            _SCANDIR, _LSTAT = real_scandir, real_lstat
        check("exit code 3 when something can't be read", code == EXIT_READ_ERROR, code)
        check("matched (incl. 5-digit ID, loose log, NFD vs NFC name, .md5)",
              len(rows(rep, "matched")) == 8, keys(rep, "matched"))
        check("missing from destination", keys(rep, "missing_from_destination") ==
              ["cabc/cabc_000003/cabc_000003_gone.tif"], keys(rep, "missing_from_destination"))
        check("extra in destination", keys(rep, "extra_in_destination") ==
              ["cabc/cabc_000001/cabc_000001_extra.tif"])
        sm = rows(rep, "size_mismatch")
        check("size mismatch", len(sm) == 1 and sm[0]["size_difference"] == "1")
        check("duplicate across sources (reported, not picked)",
              keys(rep, "duplicate_across_sources") == ["cdup/cdup_000001/cdup_000001_prsv.tif"]
              and len(rows(rep, "duplicate_across_sources")) == 3)
        check("names differing only in case flagged", len(rows(rep, "case_conflict")) == 2)
        un = rows(rep, "unreadable")
        check("unreadable file and folder reported", len(un) == 2, len(un))
        nc = [r["full_path"] for r in rows(rep, "not_compared")]
        check("folder outside a MARC folder not compared", any("cleanup" in p for p in nc), nc)
        check("junk skipped and counted", "._* x1" in text and ".Trashes (folder; contents not listed) x1" in text
              and "*.pkf x1" in text)
        with open(os.path.join(rep, "summary.csv"), encoding="utf-8-sig") as f:
            summ = list(csv.DictReader(f))
        tot = [r for r in summ if r["level"] == "TOTAL"][0]
        check("summary has file counts and GB", tot["expected_files"] == "13" and tot["destination_GB"] != "",
              (tot["expected_files"], tot["destination_GB"]))
        check("summary has MARC and object rows", any(r["level"] == "MARC" and r["marc_code"] == "cabc" for r in summ)
              and any(r["object_identifier"] == "cabc_00002" for r in summ))
        try:
            import openpyxl  # noqa: F401
            check(".xlsx written", any(n.endswith(".xlsx") for n in os.listdir(rep)))
        except ImportError:
            check(".xlsx skipped cleanly without openpyxl", "openpyxl is not installed" in text)
        code, rep, _ = go(["interim", "--source", S1, "--source", src2, "--dest", D])
        check("exit code 1 for discrepancies", code == EXIT_DISCREPANCY, code)
        code, rep, _ = go(["interim", "--source", D, "--dest", D])
        check("exit code 0 when everything matches", code == EXIT_OK, code)
        empty = T("EMPTY")
        os.makedirs(empty)
        code, _, _ = go(["interim", "--source", empty, "--dest", D])
        check("exit code 2 when no MARC folders are found", code == EXIT_USAGE, code)
        code, _, _ = go(["interim", "--dest", D])
        check("exit code 2 when an input is missing and nobody can be asked", code == EXIT_USAGE, code)
        KNOWN_TAPE_MOUNTS.append(D)
        code, _, text = go(["interim", "--source", S1, "--dest", D])
        KNOWN_TAPE_MOUNTS[:] = saved_mounts
        check("live tape refused (exit 2)", code == EXIT_USAGE and "live tape" in text, code)
        code, _, text = go(["interim", "--source", S1, "--dest", D, "--out", os.path.join(D, "reports")])
        check("reports refused inside a folder being compared", code == EXIT_USAGE)
        check("nothing in the compared folders changed", snapshot(S1, D) == before)

        # -- job B: partner -------------------------------------------------
        print("partner job")
        V = T("VENDOR")
        mov = b"m" * 100
        side = b"0cc175b9c0f1b6a831c399e269772661 *cpar_000001_prsv.mov\n"
        mk(os.path.join(V, "Film/cpar/cpar_000001/cpar_000001_prsv.mov"), data=mov)
        mk(os.path.join(V, "Film/cpar/cpar_000001/cpar_000001_prsv.mov.md5"), data=side)
        mk(os.path.join(V, "Audio/cpar/cpar_000002/cpar_000002_prsv.wav"), 80)
        P = T("PARTNER")
        mk(os.path.join(P, "cpar/cpar_000001/cpar_000001_prsv.mov"), data=mov)
        mk(os.path.join(P, "cpar/cpar_000001/cpar_000001_prsv.mov.md5"), data=side)
        mk(os.path.join(P, "cpar/cpar_000002/cpar_000002_prsv.wav"), 80)
        mk(os.path.join(P, "ReadMe_cpar.txt"), 30)
        mk(os.path.join(P, "cpar_allCycles_MetadataForPartner.csv"), 30)
        mk(os.path.join(P, "Md5tool/md5tool.py"), 30)
        mk(os.path.join(P, "$RECYCLE.BIN/desktop.ini"), 3)
        srcv = T("source_inventory.tsv")
        tsv(srcv, [(100, "SRCV/cpar/cpar_000001/cpar_000001_prsv.mov"),
                   (len(side), "SRCV/cpar/cpar_000001/cpar_000001_prsv.mov.md5"),
                   (80, "SRCV/cpar/cpar_000002/cpar_000002_prsv.wav")])
        code, _, text = go(["partner", "--source-list", srcv, "--vendor", V, "--partner", P])
        check("MARC folder in several places needs a choice (exit 2)", code == EXIT_USAGE, code)
        code, rep, _ = go(["partner", "--source-list", srcv, "--vendor", V, "--partner", P, "--within", "Film"])
        check("--within picks one place", code == EXIT_DISCREPANCY and
              keys(rep, "missing_from_destination") == ["cpar/cpar_000002/cpar_000002_prsv.wav"], code)
        code, rep, text = go(["partner", "--source-list", srcv, "--vendor", V, "--partner", P,
                              "--all-parents", "--write-inventory", "--yes"])
        check("vendor drive split by format merged; all three match (exit 0)", code == EXIT_OK, code)
        nc = [r["full_path"] for r in rows(rep, "not_compared")]
        check("ReadMe, metadata CSV and Md5tool not counted as discrepancies",
              any("ReadMe_cpar.txt" in p for p in nc) and any("Md5tool" in p for p in nc))
        inv = [n for n in os.listdir(P) if n.endswith(".csv") and "_Inventory_" in n]
        ok = False
        if inv:
            with open(os.path.join(P, inv[0]), encoding="utf-8-sig") as f:
                inv_rows = list(csv.DictReader(f))
            ok = any(r["filename"] == "cpar_000001_prsv.mov" and r["md5"] == "0cc175b9c0f1b6a831c399e269772661"
                     and r["object_identifier"] == "cpar_000001" for r in inv_rows)
        check("partner inventory written at the drive root with md5 from the sidecar", ok)
        paths = T("partner_paths.csv")
        with open(paths, "w", encoding="utf-8") as f:
            for rel in ("cpar/cpar_000001/cpar_000001_prsv.mov", "cpar/cpar_000001/cpar_000001_prsv.mov.md5",
                        "cpar/cpar_000002/cpar_000002_prsv.wav", "ReadMe_cpar.txt", ".Spotlight-V100/x"):
                f.write("/Volumes/PARTNER_GG/{0}\n".format(rel))
        code, rep, text = go(["partner", "--source-list", srcv, "--vendor", V, "--partner", paths, "--all-parents"])
        check("path-only list accepted, size check skipped and noted",
              code == EXIT_OK and "size not checked for 3 files" in text, code)
        mk(os.path.join(V, "Audio/cpar/cpar_000001/cpar_000001_prsv.mov"), data=mov)
        code, rep, _ = go(["partner", "--source-list", srcv, "--vendor", V, "--partner", P, "--all-parents"])
        check("same file in two places on the vendor drive is a duplicate (exit 1)",
              code == EXIT_DISCREPANCY and "cpar/cpar_000001/cpar_000001_prsv.mov" in
              keys(rep, "duplicate_across_sources"), code)

        # -- job C: tape ----------------------------------------------------
        print("tape job")
        src_rows = [(100, "DRV/Write2T/cfoo/cfoo_000001/cfoo_000001_prsv.tif"),
                    (45, "DRV/Write2T/cfoo/cfoo_000001/cfoo_000001_prsv.tif.md5"),
                    (200, "DRV/Write2T/cfoo/cfoo_000002/cfoo_000002_prsv.tif"),
                    (45, "DRV/Write2T/cfoo/cfoo_000002/cfoo_000002_prsv.tif.md5"),
                    (5, "DRV/.Trashes/501/x.tif"), (4, "DRV/2024-2025_raw/notes.txt")]
        tsrc = T("tape_source.tsv")
        tsv(tsrc, src_rows)
        new = [(s, p.replace("DRV/Write2T/", "")) for s, p in src_rows if "Write2T" in p]

        def tape_list(path, vol, entries):
            tsv(path, [(s, vol + "/" + p) for s, p in entries])

        old = [(300, "cold/cold_000001/cold_000001_prsv.tif")]
        tape_list(T("A_pre.tsv"), "TAPE0001L8A", old + [(6, "cold/.DS_Store")])
        tape_list(T("B_pre.tsv"), "TAPE0001L8B", old)
        old_md5 = [(45, "cold/cold_000001/cold_000001_prsv.tif.md5")]
        tape_list(T("A_post.tsv"), "TAPE0001L8A", old + old_md5 + [(6, "cold/.DS_Store")] + new)
        tape_list(T("B_post.tsv"), "TAPE0001L8B", old + old_md5 + new)
        base = ["tape", "--source", tsrc, "--a-pre", T("A_pre.tsv"), "--b-pre", T("B_pre.tsv")]
        code, rep, text = go(base + ["--a-post", T("A_post.tsv"), "--b-post", T("B_post.tsv")])
        check("post-write = pre-write + source on both tapes, A mirrors B (exit 0)", code == EXIT_OK, code)
        check("old pre-write lists without .md5 handled and noted", "left out of this comparison" in text)
        check("report folder named after the tape set", "TAPE0001L8" in os.path.basename(rep))

        bad_new = [(101 if "000001_prsv.tif" == p[-15:] else s, p) for s, p in new
                   if "cfoo_000002_prsv.tif" not in p or p.endswith(".md5")]
        tape_list(T("A_post_bad.tsv"), "TAPE0001L8A",
                  old + old_md5 + bad_new + [(9, "cfoo/cfoo_000001/cfoo_000001_extra.tif")])
        tape_list(T("B_post_bad.tsv"), "TAPE0001L8B", old_md5 + new)
        dup_src = T("tape_source_dup.tsv")
        tsv(dup_src, src_rows + [(300, "DRV/Write2T/cold/cold_000001/cold_000001_prsv.tif")])
        code, rep, text = go(["tape", "--source", dup_src, "--a-pre", T("A_pre.tsv"), "--b-pre", T("B_pre.tsv"),
                              "--a-post", T("A_post_bad.tsv"), "--b-post", T("B_post_bad.tsv")])
        check("tape discrepancies give exit 1", code == EXIT_DISCREPANCY, code)
        check("tape A: missing file found", "cfoo/cfoo_000002/cfoo_000002_prsv.tif" in
              keys(rep, "missing_from_destination", "A:"))
        check("tape A: extra file found", keys(rep, "extra_in_destination", "A:") ==
              ["cfoo/cfoo_000001/cfoo_000001_extra.tif"])
        check("tape A: size mismatch found", keys(rep, "size_mismatch", "A:") ==
              ["cfoo/cfoo_000001/cfoo_000001_prsv.tif"])
        check("file already on tape and in the source is a duplicate",
              "cold/cold_000001/cold_000001_prsv.tif" in keys(rep, "duplicate_across_sources", "A:"))
        check("tape B: pre-write file missing after the write", "cold/cold_000001/cold_000001_prsv.tif" in
              keys(rep, "duplicate_across_sources", "B:") + keys(rep, "missing_from_destination", "B:"))
        mirror_only_a = keys(rep, "missing_from_destination", "A post-write <->")
        mirror_only_b = keys(rep, "extra_in_destination", "A post-write <->")
        check("A <-> B: files only on A and only on B", "cold/cold_000001/cold_000001_prsv.tif" in mirror_only_a
              and "cfoo/cfoo_000002/cfoo_000002_prsv.tif" in mirror_only_b, (mirror_only_a, mirror_only_b))
        check("A <-> B: size difference found", "cfoo/cfoo_000001/cfoo_000001_prsv.tif" in
              keys(rep, "size_mismatch", "A post-write <->"))
        tape_list(T("A_first.tsv"), "TAPE0002L8A", new)
        tape_list(T("B_first.tsv"), "TAPE0002L8B", new)
        code, _, _ = go(["tape", "--source", tsrc, "--a-first-write", "--b-first-write",
                         "--a-post", T("A_first.tsv"), "--b-post", T("B_first.tsv")])
        check("first write: post-write must equal the source (exit 0)", code == EXIT_OK, code)
        code, _, _ = go(["tape", "--source", tsrc, "--a-post", T("A_first.tsv"), "--b-post", T("B_first.tsv")])
        check("missing pre-write list without --first-write is an input error (exit 2)", code == EXIT_USAGE, code)
        code, _, text = go(base + ["--a-post", D, "--b-post", T("B_post.tsv")])
        check("a folder given as a tape input is refused (exit 2)", code == EXIT_USAGE and "never reads a tape" in text)
    finally:
        UI.interactive = saved_interactive
        KNOWN_TAPE_MOUNTS[:] = saved_mounts
        _SCANDIR, _LSTAT = os.scandir, os.lstat
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    failed = results.count(False)
    print("Self-test: {0} passed, {1} failed".format(results.count(True), failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Nothing was changed.", file=sys.stderr)
        sys.exit(EXIT_INTERRUPTED)
