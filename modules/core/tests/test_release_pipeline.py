# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""Release version reuse, platform completeness and clean asset integrity gates."""

import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "build"))
import release_pipeline as release
import portable_bundle as bundle


def reference(tag, sha="old"):
    return {"ref": "refs/tags/" + tag, "object": {"sha": sha}}


def test_release_number_starts_at_one_and_ignores_old_calendar_tags():
    assert release.choose_version([reference("v26.11.5.1")], [], "source") == (
        "v1.0.1",
        False,
    )
    assert release.choose_version(
        [reference("v1.0.1"), reference("v1.0.3")], [], "new"
    ) == ("v1.0.4", False)


def test_rerunning_same_commit_reuses_published_or_partial_release():
    refs = [reference("v1.0.1", "source")]
    published = {"tag_name": "v1.0.1", "draft": False, "body": release.MARKER}
    assert release.choose_version(refs, [published], "source") == ("v1.0.1", True)
    assert release.choose_version(refs, [dict(published, draft=True)], "source") == (
        "v1.0.1",
        False,
    )
    assert release.choose_version(refs, [], "source") == ("v1.0.1", False)
    assert release.choose_version(
        refs, [dict(published, body="Manual release")], "source"
    ) == ("v1.0.2", False)


def test_stamp_updates_both_version_sources(tmp_path):
    directory = tmp_path / "modules/config"
    directory.mkdir(parents=True)
    (directory / "version.py").write_text("__version__ = 'v1.0.3'\n")
    (directory / "version.json").write_text('{"version":"v26.11.5.1"}')
    release.stamp("v1.0.1", tmp_path)
    assert "'v1.0.1'" in (directory / "version.py").read_text()
    assert json.loads((directory / "version.json").read_text())["version"] == "v1.0.1"
    with pytest.raises(ValueError):
        release.stamp("v26.11.5.1", tmp_path)


def test_component_subset_can_be_complete_without_unrequested_platforms(tmp_path):
    destination = tmp_path / "portable"
    destination.mkdir()
    (destination / "data").mkdir()
    executable = destination / "bin/linux-arm64/dbcheck"
    executable.parent.mkdir(parents=True)
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[18:20] = (183).to_bytes(2, "little")
    executable.write_bytes(header)
    metadata = {
        "complete": True,
        "required_platforms": ["linux-arm64"],
        "platforms": {
            "linux-arm64": {
                "executable": "bin/linux-arm64/dbcheck",
                "sha256": bundle.digest(executable),
            }
        },
    }
    (destination / "dbpulse-portable.json").write_text(json.dumps(metadata))
    bundle.archive_bundle(destination, tmp_path / "linux-arm64.zip")
    metadata["required_platforms"].append("windows-x86_64")
    (destination / "dbpulse-portable.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError):
        bundle.archive_bundle(destination, tmp_path / "incomplete.zip")


def test_unpack_rejects_archive_path_escape(tmp_path):
    archive = tmp_path / "escape.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("../outside", "fixture")
    with pytest.raises(ValueError):
        release.unpack(archive, tmp_path / "output")
    assert not (tmp_path / "outside").exists()


def assets(tmp_path, runtime=False):
    records = []
    for label in [*release.TARGETS, "Windows-Linux-Portable"]:
        path = tmp_path / ("DBPulse-v1.0.1-" + label + ".zip")
        with zipfile.ZipFile(path, "w") as package:
            package.writestr("DBPulse/source.txt", "fixture")
            if runtime:
                package.writestr("DBPulse/data/private.txt", "private")
        records.append(
            {
                "name": path.name,
                "sha256": bundle.digest(path),
                "bytes": path.stat().st_size,
                "platforms": [],
            }
        )
    manifest = {"version": "v1.0.1", "source_commit": "source", "assets": records}
    (tmp_path / "release-manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_modified_or_private_assets_block_release(tmp_path):
    manifest = assets(tmp_path)
    release.verify_assets(tmp_path, "v1.0.1", "source")
    with pytest.raises(ValueError):
        release.verify_assets(tmp_path, "v1.0.1", "wrong-commit")
    path = tmp_path / manifest["assets"][0]["name"]
    path.write_bytes(b"modified")
    with pytest.raises(ValueError):
        release.verify_assets(tmp_path, "v1.0.1", "source")
    assets(tmp_path, runtime=True)
    with pytest.raises(ValueError):
        release.verify_assets(tmp_path, "v1.0.1", "source")


def test_failed_asset_upload_leaves_release_as_draft(tmp_path):
    assets(tmp_path)

    class FakeGitHub:
        def __init__(self):
            self.operations = []

        def pages(self, path):
            return []

        def request(self, path, method="GET", data=None):
            self.operations.append((path, method, data))
            if path.startswith("git/ref/"):
                import urllib.error

                raise urllib.error.HTTPError("fixture", 404, "missing", {}, None)
            if path == "releases":
                return {"id": 1, "html_url": "https://fixture.invalid"}

        def upload(self, release_id, path):
            raise OSError("upload interrupted")

    client = FakeGitHub()
    with patch.object(release, "GitHub", return_value=client), pytest.raises(OSError):
        release.publish(tmp_path, "v1.0.1", "source")
    assert any(data and data.get("draft") is True for _, _, data in client.operations)
    assert not any(
        method == "PATCH" and data.get("draft") is False
        for _, method, data in client.operations
    )
