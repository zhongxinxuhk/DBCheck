# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Shared resource and dynamic-module inputs for native PyInstaller releases."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import json
import os
from pathlib import Path
from ci_prepare import DEFAULT_CONFIG
from modules.config.version import __version__

SKIP_DIRECTORIES = {"__pycache__", ".git", ".pytest_cache", "node_modules", "tests"}
SKIP_FILES = {".DS_Store", ".db_key", ".rbac_seeded", "bootstrap_admin_password"}
RUNTIME_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db-wal",
    ".db-shm",
    ".pyc",
    ".pyo",
    ".log",
)


def release_datas(project, directories, *, portable=False):
    project = Path(project)
    files = []
    for directory in directories:
        if directory.startswith("data/"):
            continue
        folder = project / directory
        if not folder.is_dir():
            raise FileNotFoundError("Required release resource directory: " + directory)
        for source in sorted(folder.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(project)
            if (
                SKIP_DIRECTORIES.intersection(relative.parts)
                or source.name in SKIP_FILES
                or source.name.endswith(RUNTIME_SUFFIXES)
            ):
                continue
            files.append((str(source), str(relative.parent)))
    if not portable:
        staging = Path(
            os.environ.get("DBCHECK_BUILD_INPUTS", project / "build_pyinstaller_inputs")
        )
        staging.mkdir(parents=True, exist_ok=True)
        configuration = staging / "dbc_config.json"
        config_text = json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2)
        if (
            not configuration.exists()
            or configuration.read_text(encoding="utf-8") != config_text
        ):
            configuration.write_text(config_text, encoding="utf-8")
        version = staging / "version.json"
        version_text = json.dumps({"version": __version__}, indent=2)
        if not version.exists() or version.read_text(encoding="utf-8") != version_text:
            version.write_text(version_text, encoding="utf-8")
        files.extend([(str(configuration), "."), (str(version), ".")])
    for name in ("LICENSE", "NOTICE"):
        files.append((str(project / name), "."))
    return files


def dynamic_modules(project):
    modules = []
    for source in (Path(project) / "modules").rglob("*.py"):
        relative = source.relative_to(project).with_suffix("")
        if (
            SKIP_DIRECTORIES.intersection(relative.parts)
            or "vendor" in relative.parts
            or "desktop" in relative.parts
        ):
            continue
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        if all(part.isidentifier() for part in parts):
            modules.append(".".join(parts))
    return sorted(set(modules))
