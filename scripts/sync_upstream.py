#!/usr/bin/env python3
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Selectively merge upstream functionality, preserving DBPulse presentation and edits.

Requires a clean checkout. Preview by default, --apply writes unstaged changes.
Conflicting or presentation-related files keep their previous per-file baseline.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

STATE_PATH = '.github/upstream-sync.json'
# Default deny. Frontend, translations, branding, build/release and local data
# are outside this list. Extend the list only after reviewing a new file type.
FUNCTIONAL_FILES = {'deploy/requirements.txt', 'deploy/requirements-docker.txt'}
FUNCTIONAL_FILES.update({
    'modules/config/builtin_registry.json', 'modules/config/builtin_types.json',
    'modules/config/drivers_seed.json',
})
PROTECTED_FILES = {
    'modules/core/brand.py', 'modules/config/version.py',
    'modules/config/version.json', 'modules/skill/dbcheck/scripts/version.py',
    'modules/user_management/seed.py',
}
PRESENTATION = re.compile(
    r'<\s*/?\s*[a-zA-Z!][^>]*>|\brender_template\s*\(|'
    r'\b(?:PRODUCT_\w+|RaccoonX|DBPulse)\b|'
    r'\b(?:className|innerHTML|font-family|background-color)\b'
)


def git(root, *args, check=True):
    return subprocess.run(['git', '-C', str(root), *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def eligible(path):
    parts = PurePosixPath(path).parts
    if not parts or any(p in {'.', '..'} for p in parts):
        return False
    if path in PROTECTED_FILES or 'vendor' in parts or 'static' in parts:
        return False
    if path in FUNCTIONAL_FILES:
        return True
    if parts[0] in {'modules', 'plugins'}:
        return path.endswith('.py')
    return parts[0] == 'db' and path.endswith('.sql')


def entry(root, ref, path):
    raw = git(root, 'ls-tree', '-z', ref, '--', path).stdout
    if not raw:
        return None
    metadata, name = raw.rstrip(b'\0').split(b'\t', 1)
    mode, kind, oid = metadata.decode().split()
    if kind != 'blob' or mode not in {'100644', '100755'}:
        raise ValueError(f'Unsupported file type: {path}')
    return mode, git(root, 'cat-file', 'blob', oid).stdout


def merge_entries(base, local, upstream):
    if local == upstream or base == upstream:
        return local
    if local == base:
        return upstream
    # Add/add, modify/delete, delete/modify and incompatible mode changes
    # require review. No guessed resolution and no conflict markers on disk.
    if base is None or local is None or upstream is None:
        raise ValueError('add/delete conflict')
    mode = local[0]
    if upstream[0] != base[0]:
        if local[0] != base[0] and local[0] != upstream[0]:
            raise ValueError('file mode conflict')
        mode = upstream[0]
    if any(b'\0' in item[1] for item in (base, local, upstream)):
        raise ValueError('binary content requires review')
    with tempfile.TemporaryDirectory(prefix='dbpulse-merge-') as temp:
        paths = [Path(temp) / name for name in ('local', 'base', 'upstream')]
        for path, item in zip(paths, (local, base, upstream)):
            path.write_bytes(item[1])
        merged = subprocess.run(['git', 'merge-file', '-p', '--diff3',
                                 *map(str, paths)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    if merged.returncode:
        raise ValueError('three-way merge conflict')
    return mode, merged.stdout


def sync(root, upstream_ref, apply=False):
    root = Path(root).resolve()
    if git(root, 'status', '--porcelain', '--untracked-files=all').stdout:
        raise ValueError('A clean checkout is required')
    upstream = git(root, 'rev-parse', '--verify', upstream_ref + '^{commit}').stdout.decode().strip()
    state = json.loads((root / STATE_PATH).read_text())
    previous = state['last_commit']
    # A rewritten upstream history must be reviewed instead of silently reset.
    git(root, 'merge-base', '--is-ancestor', previous, upstream)
    pending = state.get('deferred', {})
    changed = git(root, 'diff', '--name-only', '--no-renames', '-z', previous, upstream).stdout
    paths = set(changed.decode().rstrip('\0').split('\0')) - {''}
    paths.update(pending)
    report = {'upstream': upstream, 'applied': [], 'protected': [], 'deferred': {}}
    updates = {}
    deferred = {}
    for path in sorted(paths):
        if not eligible(path):
            report['protected'].append(path)
            continue
        baseline = pending.get(path, previous)
        try:
            git(root, 'merge-base', '--is-ancestor', baseline, upstream)
            base = entry(root, baseline, path)
            local = entry(root, 'HEAD', path)
            incoming = entry(root, upstream, path)
            # Mixed Python/HTML and branding edits stay for manual integration.
            delta = git(root, 'diff', '--no-ext-diff', '--no-textconv', '--unified=0',
                        baseline, upstream, '--', path).stdout.decode('utf-8')
            lines = [line[1:] for line in delta.splitlines()
                     if line.startswith(('+', '-')) and not line.startswith(('+++', '---'))]
            if any(PRESENTATION.search(line) for line in lines):
                raise ValueError('presentation or mixed presentation/functionality change')
            merged = merge_entries(base, local, incoming)
            if merged is not None and path.endswith('.py'):
                ast.parse(merged[1].decode('utf-8-sig'), filename=path)
            if merged is not None and path.endswith('.json'):
                json.loads(merged[1])
            if merged != local:
                target = root / path
                if any(parent.is_symlink() for parent in (target, *target.parents)):
                    raise ValueError('symlink path requires review')
                updates[path] = merged
                report['applied'].append(path)
        except (ValueError, SyntaxError, UnicodeError) as error:
            deferred[path] = baseline
            report['deferred'][path] = str(error)
    if apply:
        for path, item in updates.items():
            target = root / path
            if item is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(item[1])
                target.chmod(0o755 if item[0] == '100755' else 0o644)
        state.update(last_commit=upstream, deferred=deferred)
        (root / STATE_PATH).write_text(json.dumps(state, indent=2, ensure_ascii=False) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream-ref', default='refs/remotes/upstream/main')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    report = sync(root, args.upstream_ref, args.apply)
    output = json.dumps(report, indent=2, ensure_ascii=False)
    print(output)
    import os
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write('## DBPulse upstream sync\n\n```json\n' + output + '\n```\n')


if __name__ == '__main__':
    main()
