# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""Local copyright headers survive checks and automatic header repair."""

import importlib.util
from pathlib import Path
from unittest.mock import patch


def checker():
    source = Path(__file__).resolve().parents[3] / 'scripts/check_license_headers.py'
    spec = importlib.util.spec_from_file_location('license_header_fixture', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_header_survives_fix_exactly(tmp_path):
    module = checker()
    path = tmp_path / 'local.py'
    original = '\n'.join(module.LOCAL_HEADER_LINES) + '\n\n"""Local source."""\nvalue = 1\n'
    path.write_text(original)
    assert module.file_is_compliant(path)
    with patch.object(module, 'REPO_ROOT', tmp_path):
        assert not module.fix_file(Path('local.py'))
    assert path.read_text() == original


def test_local_missing_spdx_gets_local_two_line_header(tmp_path):
    module = checker()
    path = tmp_path / 'local.py'
    path.write_text(module.LOCAL_HEADER_LINES[0] + '\nvalue = 1\n')
    assert not module.file_is_compliant(path)
    with patch.object(module, 'REPO_ROOT', tmp_path):
        assert module.fix_file(Path('local.py'))
    assert path.read_text().splitlines()[:2] == module.LOCAL_HEADER_LINES
    assert module.file_is_compliant(path)


def test_original_header_and_preamble_survive_fix(tmp_path):
    module = checker()
    path = tmp_path / 'upstream.py'
    original = '#!/usr/bin/env python3\n# purpose comment\n' + '\n'.join(module.APACHE_HEADER_LINES) + '\nvalue = 1\n'
    path.write_text(original)
    with patch.object(module, 'REPO_ROOT', tmp_path):
        assert not module.fix_file(Path('upstream.py'))
    assert path.read_text() == original
    path.write_text(original.replace('sdfiyon@gmail.com', 'missing-email'))
    assert not module.file_is_compliant(path)


def test_new_unattributed_source_gets_local_header(tmp_path):
    module = checker()
    path = tmp_path / 'new_source.py'
    path.write_text('value = 1\n')
    with patch.object(module, 'REPO_ROOT', tmp_path):
        assert module.fix_file(Path('new_source.py'))
    assert path.read_text().splitlines()[:2] == module.LOCAL_HEADER_LINES
