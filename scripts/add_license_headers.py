#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Add / replace Apache 2.0 SPDX license headers in first-party Python sources.

This script walks the repository and ensures every first-party ``*.py`` file
begins (after an optional shebang and an optional encoding cookie) with:

    # SPDX-License-Identifier: Apache-2.0
    # Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
    # Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

Behaviour:
  * If a file already carries exactly that header, it is left untouched.
  * If a file carries a *different* license/copyright header (e.g. the old
    proprietary "DBCheck Professional" block, or an MIT header), the whole
    leading header block is replaced with the Apache header.
  * Otherwise the Apache header is inserted at the top.

Paths matching the exclusion rules are never touched. Run with ``--dry-run`` to
preview changes without writing.

Usage:
    python scripts/add_license_headers.py [--root DIR] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import List, Tuple

# --- Apache 2.0 header that must appear at the top of every first-party file ---
SPDX_LINE = "# SPDX-License-Identifier: Apache-2.0"
COPYRIGHT_LINE = "# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>"
AUTHOR_LINE = "# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck"
HEADER_LINES = [SPDX_LINE, COPYRIGHT_LINE, AUTHOR_LINE]

# Self-developed files carry a two-line PolyForm Noncommercial header and must
# never be rewritten to Apache-2.0 by this tool (Apache 2.0 only applies to
# upstream-derived files).
LOCAL_COPYRIGHT_LINE = "# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>"
LOCAL_SPDX_MARKER = "SPDX-License-Identifier: PolyForm"

# Default repository root = parent directory of the ``scripts/`` folder.
DEFAULT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directory name fragments that must NEVER receive a license header.
# Excludes VCS metadata, Python caches, JS/asset vendors, build artifacts,
# third-party vendored code, legal/copyright materials and runtime data dirs.
EXCLUDE_DIR_FRAGMENTS = {
    ".git",
    ".venv",
    ".idea",
    ".pytest_cache",
    ".codex",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "vendor",
    "static",
    "copyright_registration",
    "data",
    "pro_data",
    "reports",
    "snapshot",
    "awr_uploads",
}

# Marker that identifies an *existing* license/copyright header block so it can
# be replaced rather than stacked on top of.
LICENSE_MARKER_RE = re.compile(
    r"copyright|spdx-license-identifier|licen[sc]e|all rights reserved|"
    r"proprietary|&#169;|©|许可|著作权|all rights",
    re.IGNORECASE,
)

# Python encoding cookie, e.g. ``# -*- coding: utf-8 -*-`` or ``# -*- coding:utf-8 -*-``.
ENCODING_COOKIE_RE = re.compile(r"coding[:=]\s*([-\w.]+)")


def is_excluded(rel_path: str) -> bool:
    """Return True if a file path must be skipped (exclusion rules)."""
    parts = rel_path.replace(os.sep, "/").split("/")
    for part in parts:
        if part in EXCLUDE_DIR_FRAGMENTS:
            return True
    if rel_path.endswith(".pyc"):
        return True
    return False


def iter_target_files(root: str):
    """Yield ``(absolute_path, relative_posix_path)`` for every first-party .py.

    Excluded directories are pruned from the walk so they are never descended
    into.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in place to avoid descending into them.
        kept = [d for d in dirnames if d not in EXCLUDE_DIR_FRAGMENTS]
        dirnames[:] = kept
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if is_excluded(rel):
                continue
            yield full, rel


def _detect_newline(content: str) -> str:
    if "\r\n" in content:
        return "\r\n"
    return "\n"


def has_target_header(content: str) -> bool:
    """True if the file already starts with the exact Apache header.

    Accounts for an optional leading shebang and an optional encoding cookie.
    """
    lines = content.splitlines()
    idx = 0
    if lines and lines[0].startswith("#!"):
        idx = 1
    if idx < len(lines) and ENCODING_COOKIE_RE.search(lines[idx]):
        idx += 1
    if idx + 2 < len(lines):
        return (
            lines[idx] == SPDX_LINE
            and lines[idx + 1] == COPYRIGHT_LINE
            and lines[idx + 2] == AUTHOR_LINE
        )
    return False


def has_local_header(content: str) -> bool:
    """True if the file header carries the self-developed PolyForm license."""
    head = "\n".join(content.splitlines()[:80])
    return LOCAL_COPYRIGHT_LINE in head and LOCAL_SPDX_MARKER in head


def normalize_header(content: str) -> Tuple[str, bool]:
    """Return ``(new_content, changed)`` with the Apache header applied.

    The algorithm preserves an optional shebang (line 1) and an optional
    encoding cookie (line 1 or 2), strips any pre-existing license/copyright
    header block, and prepends the canonical 3-line Apache header.
    """
    nl = _detect_newline(content)
    had_trailing_nl = content.endswith(nl)
    text = content[: -len(nl)] if had_trailing_nl else content
    lines = text.split(nl) if text else []

    idx = 0
    shebang = None
    if lines and lines[0].startswith("#!"):
        shebang = lines[0]
        idx = 1

    # Preserve an encoding cookie as the first body line (must stay on line 1/2).
    encoding_line = None
    if idx < len(lines) and ENCODING_COOKIE_RE.search(lines[idx]):
        encoding_line = lines[idx]
        idx += 1

    # Identify a leading comment/blank block that may be an old license header.
    block_end = idx
    while block_end < len(lines):
        line = lines[block_end]
        if line.strip() == "":
            block_end += 1
            continue
        if line.lstrip().startswith("#"):
            block_end += 1
            continue
        break

    leading_block = lines[idx:block_end]
    has_license_marker = any(LICENSE_MARKER_RE.search(l) for l in leading_block)

    if has_license_marker:
        # Replace the entire old license header block.
        rest = lines[block_end:]
    else:
        # No old header: keep everything from here (encoding cookie already
        # captured/advanced, so no duplication).
        rest = lines[idx:]

    out: List[str] = []
    if shebang is not None:
        out.append(shebang)
    if encoding_line is not None:
        out.append(encoding_line)
    out.extend(HEADER_LINES)
    if rest:
        out.append("")
    out.extend(rest)

    new_content = nl.join(out)
    if had_trailing_nl:
        new_content += nl

    return new_content, new_content != content


def add_headers(root: str, dry_run: bool = False) -> dict:
    """Process every first-party .py under ``root``.

    Returns a summary dict with counts and the list of changed files.
    """
    changed_files: List[str] = []
    skipped_ok = 0
    skipped_local = 0
    processed = 0
    for full, rel in iter_target_files(root):
        processed += 1
        try:
            with open(full, "r", encoding="utf-8-sig") as fh:
                content = fh.read()
        except (UnicodeDecodeError, OSError) as exc:
            print(f"[WARN] skipped unreadable file {rel}: {exc}", file=sys.stderr)
            continue

        if has_target_header(content):
            skipped_ok += 1
            continue

        if has_local_header(content):
            # Self-developed file: keep its PolyForm Noncommercial header.
            skipped_local += 1
            continue

        new_content, changed = normalize_header(content)
        if not changed:
            skipped_ok += 1
            continue

        changed_files.append(rel)
        if not dry_run:
            with open(full, "w", encoding="utf-8", newline="") as fh:
                fh.write(new_content)

    summary = {
        "processed": processed,
        "changed": len(changed_files),
        "unchanged": skipped_ok,
        "skipped_local": skipped_local,
        "changed_files": changed_files,
        "dry_run": dry_run,
    }
    return summary


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=DEFAULT_ROOT,
        help="Repository root to scan (default: parent of scripts/).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing any files.",
    )
    args = parser.parse_args(argv)

    summary = add_headers(args.root, dry_run=args.dry_run)

    action = "would change" if args.dry_run else "changed"
    print(f"Scanned first-party .py files : {summary['processed']}")
    print(f"Already correct (skipped)     : {summary['unchanged']}")
    print(f"Self-developed (kept PolyForm): {summary['skipped_local']}")
    print(f"Files {action}               : {summary['changed']}")
    for rel in summary["changed_files"]:
        print(f"  - {rel}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
