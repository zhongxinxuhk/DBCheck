#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""
check_license_headers.py

License-header compliance checker and auto-fixer for the DBCheck repository.

Pure standard library (Python 3.8+). No third-party dependencies.

What it does
------------
* Walks the repository (relative to the Git/working-tree root, by default the
  parent directory of this script) and collects all first-party ``*.py`` files.
* Skips vendored / generated / data directories and any file that already
  carries a genuine non-Apache third-party license header (for example the
  bundled ``modules/disaster_recovery/vendor/autobackup.py`` which is MIT
  licensed and must keep its own header).
* Accepts the original upstream header or the two-line copyright/SPDX header
  used by completely new local source files. Files carrying an upstream
  copyright must retain its author email and stay under Apache-2.0 (Apache 2.0
  section 4); files without upstream code use the local PolyForm Noncommercial
  1.0.0 license.
* Verifies that the root ``LICENSE`` file references the Apache License, that a
  root ``NOTICE`` file exists, and that the self-developed portions ship their
  PolyForm Noncommercial 1.0.0 text plus the commercial-license note.
* With ``--fix`` it rewrites/inserts the canonical header (Apache 2.0 for
  upstream-derived files, PolyForm Noncommercial 1.0.0 for local-only files)
  into every non-compliant first-party file.

Exit codes
----------
* 0  -> everything is compliant (or was made compliant by --fix)
* 1  -> one or more license requirements are missing
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck


from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directory name components that are always excluded (matched as a single path
# segment, case-sensitive).
EXCLUDED_DIR_COMPONENTS = {
    "__pycache__",
    ".portable-build",
    ".git",
    "node_modules",
    "dist",
    "build",
    "reports",
    "snapshot",
    "awr_uploads",
    "copyright_registration",
    "pro_data",
    "data",
    # Throwaway local test/probe dirs (never committed; keep the checker clean).
    ".acceptance_tmp",
    ".pytest_tmp",
    "_qa_tmp",
}

# Path fragments (forward-slash normalised) that mark vendored / bundled code
# which keeps its own third-party license header.
EXCLUDED_PATH_FRAGMENTS = (
    "/vendor/",       # e.g. modules/disaster_recovery/vendor/autobackup.py
    "static/vendor/",
)

# The canonical header we want at the top of every first-party file.
APACHE_HEADER_LINES = [
    "# SPDX-License-Identifier: Apache-2.0",
    "# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>",
    "# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck",
]

LOCAL_SPDX = "PolyForm-Noncommercial-1.0.0"

LOCAL_HEADER_LINES = [
    "# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>",
    f"# SPDX-License-Identifier: {LOCAL_SPDX}",
]

LOCAL_LICENSE_FILE = "LICENSE-PolyForm-Noncommercial-1.0.0.txt"
COMMERCIAL_LICENSE_FILE = "LICENSE-COMMERCIAL.md"

# A PolyForm SPDX line placed in a comment/template header. Upstream-derived
# files must never carry it, so upstream code cannot be relicensed by appending
# a local attribution block. Anchored to comment-style header lines so that a
# mere string constant mentioning the license does not trigger it.
LOCAL_SPDX_RE = re.compile(
    r"(?m)^[ \t]*(?:#|//|rem\s|/\*|\*|<!--)?[ \t]*SPDX-License-Identifier:[ \t]*PolyForm",
    re.IGNORECASE,
)
APACHE_SPDX_LINE = "SPDX-License-Identifier: Apache-2.0"

# Markers (lower-cased) that, when present in a file header together with a
# copyright holder OTHER than fiyo, indicate a genuine third-party license that
# must be preserved (not rewritten to Apache-2.0).
THIRD_PARTY_MARKERS = (
    "mit license",
    "bsd license",
    "bsd-2",
    "bsd-3",
    "gnu lesser",
    "gnu general public",
    "mozilla public license",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise(path: str) -> str:
    """Normalise a path to forward slashes for stable fragment matching."""
    return path.replace("\\", "/")


def is_excluded_by_path(rel_path: str) -> bool:
    """Return True if *rel_path* should be skipped based on its location."""
    norm = _normalise(rel_path)
    parts = Path(norm).parts
    for component in parts:
        if component in EXCLUDED_DIR_COMPONENTS:
            return True
    for frag in EXCLUDED_PATH_FRAGMENTS:
        if frag in norm:
            return True
    return False


def has_third_party_header(text: str) -> bool:
    """Detect a genuine third-party (non-Apache) license header.

    First-party DBCheck files may mention "MIT" (e.g. "MIT License with
    Attribution Requirements") but are attributed to *fiyo*, so they are NOT
    treated as third-party and will be normalised to Apache-2.0.
    """
    head = "\n".join(text.split("\n", 30)[:30]).lower()
    for marker in THIRD_PARTY_MARKERS:
        if marker in head:
            if "fiyo" not in head and "jack ge" not in head:
                return True
    return False


def collect_first_party_py_files() -> list[Path]:
    """Return a sorted list of first-party *.py files (relative to REPO_ROOT)."""
    results: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        rel_str = _normalise(str(rel))
        if is_excluded_by_path(rel_str):
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        if has_third_party_header(text):
            continue
        results.append(rel)
    results.sort(key=lambda p: _normalise(str(p)))
    return results


UPSTREAM_MARKER_RE = re.compile(
    r"(?:Copyright 2025-2026 fiyo|fiyo \(Jack Ge\)|Author: fiyo)", re.IGNORECASE
)


def header_region(text: str) -> str:
    """Return the leading region of *text* where license headers live."""
    return "\n".join(text.split("\n", 80)[:80])


def is_upstream_derived(text: str) -> bool:
    """True when the file header carries upstream fiyo attribution.

    Only the header region is inspected so that incidental mentions of the
    upstream author (for example inside fixtures or test data) do not force a
    self-developed file onto Apache-2.0.
    """
    return bool(UPSTREAM_MARKER_RE.search(header_region(text)))


def file_is_compliant(path: Path) -> bool:
    """Accept local-only headers and preserve genuine upstream attribution.

    Upstream-derived files must keep ``Apache-2.0`` plus the fiyo copyright
    email; files without upstream code must use the local PolyForm
    Noncommercial header.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return False
    has_local = LOCAL_HEADER_LINES[0] in text
    if is_upstream_derived(text):
        # Upstream-derived file: Apache-2.0 plus the upstream author email are
        # mandatory (Apache 2.0 section 4 and AGENTS.md sections 1-8), and no
        # PolyForm SPDX line may be attached to upstream code.
        head = header_region(text)
        return (
            APACHE_SPDX_LINE in head
            and "sdfiyon@gmail.com" in head
            and not LOCAL_SPDX_RE.search(text)
        )
    return (
        f"SPDX-License-Identifier: {LOCAL_SPDX}" in text
        and has_local
        and APACHE_SPDX_LINE not in header_region(text)
    )


def find_license_block(lines: list[str], start: int):
    """Find the contiguous comment/blank run starting at *start* that contains a
    license marker. Returns (block_start, block_end) or None.
    """
    n = len(lines)
    if start >= n:
        return None
    i = start
    while i < n and (lines[i].strip() == "" or lines[i].lstrip().startswith("#")):
        i += 1
    run_start, run_end = start, i
    if run_start >= run_end:
        return None
    block_text = "\n".join(lines[run_start:run_end]).lower()
    license_markers = (
        "copyright",
        "spdx-license-identifier",
        "see license",
        "released under",
        "licensed under",
        "all rights reserved",
        "proprietary software",
    )
    if any(m in block_text for m in license_markers):
        return (run_start, run_end)
    return None


def _read_raw(path: Path):
    """Read file as text, returning (text, has_bom)."""
    raw = path.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    return text, has_bom


def _write_raw(path: Path, text: str, has_bom: bool) -> None:
    """Write text back, preserving the BOM if it was present."""
    data = text.encode("utf-8")
    if has_bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)


def fix_file(rel_path: Path) -> bool:
    """Insert or replace the Apache-2.0 header. Returns True if the file changed."""
    abs_path = REPO_ROOT / rel_path
    text, has_bom = _read_raw(abs_path)
    if file_is_compliant(abs_path):
        return False
    lines = text.split("\n")

    # 1. Extract a leading shebang (must stay on line 0).
    shebang = None
    idx = 0
    if lines and lines[0].startswith("#!"):
        shebang = lines[0]
        idx = 1

    # 2. Extract a single optional coding declaration right after the shebang.
    coding = None
    if idx < len(lines) and re.match(r"^#.*coding[:=]", lines[idx].lstrip()):
        coding = lines[idx]
        idx += 1

    # 3. Detect an existing license block to replace (if any).
    block = find_license_block(lines, idx)

    # Already exactly compliant? (correct SPDX + fiyo copyright line, no stale block)
    already_apache = (
        "SPDX-License-Identifier: Apache-2.0" in text
        and bool(re.search(r"# Copyright 2025-2026 fiyo \(Jack Ge\)", text))
        and "sdfiyon@gmail.com" in text
    )
    if already_apache and block is None:
        return False

    # 4. Build the new preamble.
    preamble: list[str] = []
    if shebang is not None:
        preamble.append(shebang)
    if coding is not None:
        preamble.append(coding)
    has_upstream_marker = is_upstream_derived(text)
    is_local = not has_upstream_marker
    if is_local:
        preamble.extend(LOCAL_HEADER_LINES)
        # Keep any existing local author line so attribution is never reduced.
        if block is not None:
            block_text = "\n".join(lines[block[0]:block[1]])
            author = re.search(r"(?m)^#\s*Author: sec\.hn\.cn.*$", block_text)
            if author:
                preamble.append(author.group(0))
    else:
        preamble.extend(APACHE_HEADER_LINES)
    preamble.append("")  # single trailing blank line separating header from body

    if block is not None:
        rest = lines[block[1]:]
    else:
        rest = lines[idx:]

    # Strip leading blank lines from the remaining body to avoid double blanks.
    while rest and rest[0].strip() == "":
        rest.pop(0)

    new_lines = preamble + rest
    new_text = "\n".join(new_lines)
    if new_text == text:
        return False
    _write_raw(abs_path, new_text, has_bom)
    return True


def check_root_license_and_notice() -> list[str]:
    """Return a list of problems with the root LICENSE / NOTICE files."""
    problems: list[str] = []
    license_path = REPO_ROOT / "LICENSE"
    if not license_path.is_file():
        problems.append("Missing root LICENSE file")
    else:
        lic_text = license_path.read_text(encoding="utf-8-sig").lower()
        if "apache license" not in lic_text or "version 2.0" not in lic_text:
            problems.append(
                "Root LICENSE does not contain 'Apache License' / 'Version 2.0'"
            )
    if not (REPO_ROOT / "NOTICE").is_file():
        problems.append("Missing root NOTICE file")

    local_license = REPO_ROOT / LOCAL_LICENSE_FILE
    if not local_license.is_file():
        problems.append(f"Missing self-developed license file {LOCAL_LICENSE_FILE}")
    else:
        text = local_license.read_text(encoding="utf-8-sig").lower()
        if "polyform noncommercial license 1.0.0" not in text:
            problems.append(
                f"{LOCAL_LICENSE_FILE} does not contain the PolyForm Noncommercial 1.0.0 text"
            )
    if not (REPO_ROOT / COMMERCIAL_LICENSE_FILE).is_file():
        problems.append(f"Missing commercial-license note {COMMERCIAL_LICENSE_FILE}")
    return problems


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    global REPO_ROOT
    parser = argparse.ArgumentParser(
        description=(
            "Check/fix license headers in first-party *.py files "
            "(Apache-2.0 for upstream-derived files, PolyForm Noncommercial 1.0.0 "
            "for self-developed files)."
        )
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Insert/replace the applicable license header in non-compliant files.",
    )
    parser.add_argument(
        "--root",
        default=str(REPO_ROOT),
        help="Repository root (default: parent directory of this script).",
    )
    args = parser.parse_args(argv)

    REPO_ROOT = Path(args.root).resolve()

    files = collect_first_party_py_files()

    missing: list[str] = []
    fixed: list[str] = []

    for rel in files:
        abs_path = REPO_ROOT / rel
        if args.fix:
            if fix_file(rel):
                fixed.append(_normalise(str(rel)))
            if not file_is_compliant(abs_path):
                missing.append(_normalise(str(rel)))
        else:
            if not file_is_compliant(abs_path):
                missing.append(_normalise(str(rel)))

    root_problems = check_root_license_and_notice()

    if args.fix:
        if fixed:
            print(f"FIX: wrote license headers to {len(fixed)} file(s):")
            for f in fixed:
                print(f"  + {f}")
        else:
            print("FIX: 0 files needed changes (all first-party files compliant).")

    if missing:
        print("\nMissing license headers in the following first-party file(s):")
        for f in missing:
            print(f"  - {f}")
        print(f"\nFAIL: {len(missing)} file(s) missing required license header.")
        for p in root_problems:
            print(f"  - {p}")
        return 1

    if root_problems:
        print("\nRoot license file problems:")
        for p in root_problems:
            print(f"  - {p}")
        print("\nFAIL: root license/NOTICE check failed.")
        return 1

    print(f"OK: {len(files)} files checked, 0 missing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
