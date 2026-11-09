#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Dependency-free checks for fork branding, update links and release paths."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.core.brand import PRODUCT_NAME_EN, PRODUCT_NAME_ZH, SYSTEM_UPDATE_URL
from i18n import _LANGS, get_all_translations, t


def main():
    assert PRODUCT_NAME_EN == 'DBPulse'
    assert PRODUCT_NAME_ZH == '数据库智能巡检系统'
    assert SYSTEM_UPDATE_URL == 'https://github.com/zhongxinxuhk/DBCheck/releases'
    for lang in _LANGS:
        data = get_all_translations(lang)
        assert data['login.title'] == 'DBPulse', lang
        assert 'RaccoonX' in data['login.copyright'], lang
        assert 'fiyo' in data['login.copyright'], lang
        assert t('login.title', lang) == data['login.title']
        assert data['webui.about_grayscale_note'] == _LANGS[lang]['webui.about_grayscale_note'], lang
        assert data['webui.license_key_placeholder'] == _LANGS[lang]['webui.license_key_placeholder'], lang
    assert 'DBCheck/drivers/' in t('webui.yashandb_driver_desc', 'en')
    template = (ROOT / 'web_templates/index.html').read_text(encoding='utf-8-sig')
    assert 'id="system-update-link" href="{{ system_update_url }}"' in template
    assert 'RaccoonX / DBCheck' in template and 'sdfiyon@gmail.com' in template
    assert 'https://github.com/fiyo/DBCheck' in template
    # Verify all packaging producers and consumers agree on the renamed paths.
    for platform in ('windows', 'linux', 'macos'):
        text = (ROOT / 'build' / f'dbcheck_{platform}.spec').read_text()
        ast.parse(text)
        assert "name='DBPulse-" in text
    for name in ('build-release.yml', 'rebuild-linux.yml'):
        text = (ROOT / '.github/workflows' / name).read_text()
        assert 'dist/DBPulse-' in text
        assert 'dist/RaccoonX-' not in text
    for name in ('docker.yml', 'docker-multiarch.yml'):
        text = (ROOT / '.github/workflows' / name).read_text()
        assert 'ghcr.io/zhongxinxuhk/dbpulse' in text
        assert 'ghcr.io/fiyo/dbcheck' not in text
        assert 'jackge12345/dbcheck' not in text
    print(f'OK: DBPulse branding in {len(_LANGS)} languages, update URL, attribution and release paths')


if __name__ == '__main__':
    main()
