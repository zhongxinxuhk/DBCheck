# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Release version reuse, platform completeness and clean asset integrity gates."""

import importlib.util
import io
import json
import subprocess
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
    with patch.object(release, "GitHub", return_value=client), patch.object(
        release, "release_notes", return_value="fixture iteration details"
    ), pytest.raises(OSError):
        release.publish(tmp_path, "v1.0.1", "source")
    assert any(data and data.get("draft") is True for _, _, data in client.operations)
    assert not any(
        method == "PATCH" and data.get("draft") is False
        for _, method, data in client.operations
    )


def test_invalid_driver_is_closed_before_windows_cleanup(tmp_path):
    root = tmp_path / "source"
    (root / "drivers/gbase8a/legacy").mkdir(parents=True)
    (root / "modules/config").mkdir(parents=True)
    (root / "modules/config/version.json").write_text('{"version":"v1.0.1"}')
    (root / "build/portable").mkdir(parents=True)
    for name in ("start.sh", "start.bat"):
        (root / "build/portable" / name).write_text("fixture")
    for name in ("LICENSE", "NOTICE"):
        (root / name).write_text("fixture")
    with zipfile.ZipFile(root / "drivers/gbase8a/legacy/invalid.jar", "w") as archive:
        archive.writestr("incorrect/Driver.class", "fixture")
    opened = []
    real_zip = zipfile.ZipFile
    real_unlink = Path.unlink

    def tracked_zip(path, *args, **kwargs):
        archive = real_zip(path, *args, **kwargs)
        opened.append(archive)
        return archive

    def windows_unlink(path, *args, **kwargs):
        assert all(archive.fp is None for archive in opened)
        return real_unlink(path, *args, **kwargs)

    with patch.object(bundle, "ROOT", root), patch.object(
        zipfile, "ZipFile", tracked_zip
    ), patch.object(Path, "unlink", windows_unlink):
        bundle.stage_resources(tmp_path / "staged", ["windows-x86_64"])
    assert not (tmp_path / "staged/drivers/gbase8a/legacy/invalid.jar").exists()


def test_verified_download_retries_transport_and_keeps_cache_atomic(tmp_path):
    import io
    import urllib.error

    payload = b"verified build input"
    checksum = __import__("hashlib").sha256(payload).hexdigest()
    archive = tmp_path / "input.zip"
    with patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=[urllib.error.URLError("handshake timeout"), io.BytesIO(payload)],
    ) as request, patch.object(bundle.time, "sleep"):
        assert (
            bundle.download_verified("https://fixture.invalid", archive, checksum)
            == archive
        )
    assert archive.read_bytes() == payload
    assert request.call_count == 2
    archive.write_bytes(b"previous cache entry")
    with patch.object(
        bundle.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")
    ), patch.object(bundle.time, "sleep"), pytest.raises(urllib.error.URLError):
        bundle.download_verified("https://fixture.invalid", archive, checksum)
    assert archive.read_bytes() == b"previous cache entry"
    assert not list(tmp_path.glob("*.download"))


def test_verified_download_never_accepts_wrong_checksum(tmp_path):
    import io

    archive = tmp_path / "input.zip"
    with patch.object(
        bundle.urllib.request,
        "urlopen",
        side_effect=lambda *args, **kwargs: io.BytesIO(b"wrong"),
    ), patch.object(bundle.time, "sleep"), pytest.raises(ValueError):
        bundle.download_verified("https://fixture.invalid", archive, "0" * 64)
    assert not archive.exists()


def test_extraction_normalizes_alias_paths_before_containment_checks(tmp_path):
    import os

    if os.name == "nt":
        pytest.skip("POSIX alias fixture is covered by the Windows native transfer job")
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    archive = tmp_path / "portable.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("DBPulse/dbpulse-portable.json", "{}")
    extracted = release.unpack(archive, alias / "extracted")
    assert extracted == extracted.resolve()
    fixture = tmp_path / "state.zip"
    with zipfile.ZipFile(fixture, "w") as package:
        package.writestr("data/fixture.json", "{}")
    result = {"ok": True, "source_platform": "windows-x86_64"}
    aliased_bundle = alias / "extracted/DBPulse"
    with patch.object(release, "unpack", return_value=aliased_bundle), patch.object(
        release, "run_check", return_value=result
    ):
        release.transfer_verify(
            archive, fixture, "windows-x86_64", tmp_path / "verification.json"
        )
    assert (physical / "extracted/DBPulse/data/fixture.json").is_file()


@pytest.fixture
def notes_repository(tmp_path):
    def git(*arguments):
        return subprocess.check_output(
            ["git", *arguments], cwd=tmp_path, text=True, encoding="utf-8"
        ).strip()

    git("init", "-q")
    git("config", "user.name", "Release tests")
    git("config", "user.email", "release@example.invalid")
    git("commit", "--allow-empty", "-qm", "feat: 已发布的旧功能")
    git("tag", "-a", "v1.0.1", "-m", "previous release")
    for subject in (
        "feat(desktop): 增加授权客户名称",
        "refactor(ui): 调整授权信息布局",
        "fix(inspection): 修复巡检报告生成失败",
        "docs: 补充升级说明",
    ):
        git("commit", "--allow-empty", "-qm", subject)
    return tmp_path


def test_notes_group_actual_iteration_and_exclude_previous_release(notes_repository):
    notes = release.release_notes("v1.0.2", "HEAD", "v1.0.1", notes_repository)
    assert "已发布的旧功能" not in notes
    assert "### 新增功能\n\n- feat(desktop): 增加授权客户名称" in notes
    assert "### 功能改动\n\n- refactor(ui): 调整授权信息布局" in notes
    assert "### BUG 修复\n\n- fix(inspection): 修复巡检报告生成失败" in notes
    assert "### 其他更新\n\n- docs: 补充升级说明" in notes
    baseline = release.git_output(notes_repository, "rev-parse", "v1.0.1^{commit}")
    assert "/compare/" + baseline + "..." in notes
    assert "DBPulse-v1.0.2-Windows-Linux-Portable.zip" in notes


def test_notes_use_exact_version_and_ignore_stale_unreleased(notes_repository):
    path = notes_repository / "CHANGELOG.md"
    path.write_text(
        "## 未发布\n\n- 过期未发布说明\n\n"
        "## v1.0.20\n\n- 其他版本内容\n\n"
        "## [v1.0.2] (2026-11-13)\n\n"
        "### 新增功能\n\n- 支持客户姓名及单位展示。\n\n"
        "### BUG 修复\n\n- 修复报告输出问题。\n\n"
        "## v1.0.1\n\n- 上版详情\n", encoding="utf-8",
    )
    notes = release.release_notes("v1.0.2", "HEAD", "v1.0.1", notes_repository)
    assert "支持客户姓名及单位展示。" in notes
    assert "修复报告输出问题。" in notes
    assert "feat(desktop)" not in notes
    assert "其他版本内容" not in notes
    assert "过期未发布说明" not in notes
    assert "上版详情" not in notes
    assert release.changelog_section("v1.0.3", notes_repository) == ""


def test_notes_initial_release_and_empty_iteration(notes_repository):
    first = release.release_notes("v1.0.2", "HEAD", root=notes_repository)
    assert "首次正式发布" in first
    assert "已发布的旧功能" in first
    empty = release.release_notes("v1.0.2", "HEAD", "HEAD", notes_repository)
    assert empty.count("本次无相关提交记录。") == 4


def test_previous_release_excludes_draft_prerelease_and_unrelated_branch(notes_repository):
    def git(*arguments):
        return release.git_output(notes_repository, *arguments)

    head = git("rev-parse", "HEAD")
    git("tag", "v1.0.2")
    git("checkout", "--orphan", "unrelated")
    git("commit", "--allow-empty", "-qm", "unrelated release")
    git("tag", "v1.0.8")
    releases = [
        {"tag_name": "v1.0.1", "draft": False, "published_at": "2026-11-11"},
        {"tag_name": "v1.0.2", "draft": True, "published_at": "2026-11-12"},
        {"tag_name": "v1.0.8", "draft": False, "published_at": "2026-11-13"},
        {"tag_name": "v1.0.9", "draft": False, "prerelease": True},
    ]
    assert release.previous_release(releases, "v1.0.3", head, notes_repository) == "v1.0.1"
    assert release.previous_release([], "v1.0.3", head, notes_repository) == ""


def test_publish_uses_same_notes_for_draft_final_release_and_local_preview(tmp_path):
    assets(tmp_path)

    class FakeGitHub:
        def __init__(self):
            self.operations = []

        def pages(self, path):
            return []

        def request(self, path, method="GET", data=None):
            self.operations.append((path, method, data))
            if path.startswith("git/ref/"):
                return {"object": {"sha": "source"}}
            return {"id": 1, "html_url": "https://fixture.invalid"}

        def upload(self, release_id, path):
            pass

    client = FakeGitHub()
    notes = "### 新增功能\n\n- 客户名称展示\n\n### BUG 修复\n\n- 修复报告输出"
    with patch.object(release, "GitHub", return_value=client), patch.object(
        release, "release_notes", return_value=notes
    ):
        release.publish(tmp_path, "v1.0.1", "source")
    bodies = [data["body"] for _, _, data in client.operations if data and "body" in data]
    assert bodies == [notes, notes]
    assert (tmp_path / "release-notes.md").read_text(encoding="utf-8") == notes


def test_notes_failure_happens_before_remote_writes(tmp_path):
    assets(tmp_path)
    from unittest.mock import Mock

    client = Mock()
    client.pages.return_value = []
    client.request.return_value = {"object": {"sha": "source"}}
    with patch.object(release, "GitHub", return_value=client), patch.object(
        release, "release_notes", side_effect=ValueError("invalid notes source")
    ), pytest.raises(ValueError, match="invalid notes source"):
        release.publish(tmp_path, "v1.0.1", "source")
    assert all(call.args[0].startswith("git/ref/") for call in client.request.call_args_list)
    client.upload.assert_not_called()


def test_workflow_builds_and_verifies_only_windows_and_linux():
    import yaml

    workflow = yaml.safe_load((ROOT / '.github/workflows/portable-release.yml').read_text())
    for job_name in ('native', 'verify-transfer'):
        matrix = workflow['jobs'][job_name]['strategy']['matrix']['include']
        assert {item['platform'] for item in matrix} == set(release.TARGETS)
        assert all(not item['runner'].startswith('macos') for item in matrix)
    notes = release.release_notes('v1.0.2', 'HEAD', 'HEAD', root=ROOT)
    assert 'macos-arm64.zip' not in notes
    assert 'macOS' not in notes


def test_release_requires_exact_windows_linux_packages(tmp_path):
    manifest = assets(tmp_path)
    assert len(manifest['assets']) == 4
    release.verify_assets(tmp_path, 'v1.0.1', 'source')
    manifest['assets'][-1] = manifest['assets'][0]
    (tmp_path / 'release-manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='Windows x64, Linux x64, Linux ARM64'):
        release.verify_assets(tmp_path, 'v1.0.1', 'source')


class ArtifactCleanupGitHub:
    def __init__(self):
        self.sha = 'a' * 40
        self.tag = 'v1.0.8'
        names = {f'DBPulse-{self.tag}-{platform}.zip' for platform in (*release.TARGETS, 'Windows-Linux-Portable')}
        names.update({'release-manifest.json', 'SHA256SUMS.txt'})
        self.release = {'tag_name': self.tag, 'draft': False, 'prerelease': False,
                        'body': release.MARKER + '\n源提交为 `' + self.sha + '`。',
                        'assets': [{'name': name, 'state': 'uploaded', 'size': 100} for name in names]}
        self.runs = {17: {'head_sha': self.sha, 'status': 'completed', 'conclusion': 'success',
                         'path': '.github/workflows/build-release.yml', 'head_branch': 'main'},
                     18: {'head_sha': self.sha, 'status': 'completed', 'conclusion': 'failure',
                         'path': '.github/workflows/build-release.yml', 'head_branch': 'main'}}
        self.artifacts = [
            {'id': 1, 'name': 'release-candidate-' + self.tag, 'expired': False, 'size_in_bytes': 100,
             'workflow_run': {'id': 17, 'head_sha': self.sha}},
            {'id': 2, 'name': 'customer-evidence', 'expired': False, 'size_in_bytes': 100,
             'workflow_run': {'id': 17, 'head_sha': self.sha}},
            {'id': 3, 'name': 'windows-transfer-fixture', 'expired': False, 'size_in_bytes': 100,
             'workflow_run': {'id': 18, 'head_sha': self.sha}},
        ]
        self.deleted = []

    def pages(self, path):
        assert path == 'releases'
        return [self.release]

    def request(self, path, method='GET', data=None):
        if path.startswith('git/ref/tags/'):
            return {'object': {'type': 'commit', 'sha': self.sha}}
        if path.startswith('actions/artifacts?'):
            return {'artifacts': self.artifacts}
        if path.startswith('actions/runs/'):
            return self.runs[int(path.rsplit('/', 1)[1])]
        assert method == 'DELETE'
        self.deleted.append(int(path.rsplit('/', 1)[1]))


def test_artifact_cleanup_requires_exact_published_assets_and_successful_run():
    github = ArtifactCleanupGitHub()
    assert release.cleanup_artifacts(github)['selected'] == 1
    assert github.deleted == []
    assert release.cleanup_artifacts(github, apply=True)['removed'] == 1
    assert github.deleted == [1]


@pytest.mark.parametrize('change', ['draft', 'missing_asset', 'wrong_source', 'other_workflow', 'failed_run'])
def test_artifact_cleanup_preserves_unverified_or_unrelated_artifacts(change):
    github = ArtifactCleanupGitHub()
    if change == 'draft':
        github.release['draft'] = True
    elif change == 'missing_asset':
        github.release['assets'].pop()
    elif change == 'wrong_source':
        github.release['body'] = release.MARKER + '\n源提交为 `' + 'b' * 40 + '`。'
    elif change == 'other_workflow':
        github.runs[17]['path'] = '.github/workflows/other.yml'
    else:
        github.runs[17]['conclusion'] = 'failure'
    assert release.cleanup_artifacts(github, apply=True)['removed'] == 0
    assert github.deleted == []


def test_artifact_cleanup_allows_only_its_own_published_in_progress_run(monkeypatch):
    github = ArtifactCleanupGitHub()
    github.runs[17].update(status='in_progress', conclusion=None)
    assert release.cleanup_artifacts(github, apply=True, current_run=17)['removed'] == 0
    monkeypatch.setenv('GITHUB_RUN_ID', '17')
    monkeypatch.setenv('GITHUB_SHA', github.sha)
    assert release.cleanup_artifacts(github, apply=True, current_run=17)['removed'] == 1
    assert github.deleted == [1]


class StagingGitHub:
    repository = 'fixture/project'
    token = 'fixture-token'

    def __init__(self):
        self.releases = []
        self.assets = []
        self.content = {}
        self.deleted = []

    def pages(self, path):
        return self.releases if path == 'releases' else self.assets

    def request(self, path, method='GET', data=None):
        if path == 'releases' and method == 'POST':
            item = dict(data, id=71)
            self.releases.append(item)
            return item
        if method == 'DELETE':
            self.deleted.append(path)
            if path.startswith('releases/assets/'):
                self.assets = [a for a in self.assets if str(a['id']) != path.rsplit('/', 1)[1]]
            return None
        if path.startswith('git/ref/'):
            import urllib.error
            raise urllib.error.HTTPError(path, 404, 'missing', {}, None)
        raise AssertionError(path)

    def upload(self, release_id, path, *, label=None):
        content = path.read_bytes()
        self.content[path.name] = content
        self.assets.append({'id': len(self.assets) + 1, 'name': path.name, 'size': len(content),
                            'state': 'uploaded', 'label': label, 'digest': 'sha256:' + release.digest(path)})


@pytest.fixture
def staging_environment(monkeypatch):
    monkeypatch.setenv('GITHUB_RUN_ID', '77')
    monkeypatch.setenv('GITHUB_SHA', 'a' * 40)


def test_staging_is_private_run_scoped_and_rejects_other_sources(staging_environment):
    github = StagingGitHub()
    item = release.staging_release(github, create=True)
    assert item['draft'] and item['prerelease']
    assert item['tag_name'] == 'ci-build-77'
    assert release.staging_release(github)['id'] == item['id']
    item['body'] = item['body'].replace('a' * 40, 'b' * 40)
    with pytest.raises(ValueError, match='identity'):
        release.staging_release(github)


def test_staging_round_trip_preserves_only_selected_paths(tmp_path, staging_environment):
    github = StagingGitHub()
    release.staging_release(github, create=True)
    root = tmp_path / 'source'
    (root / 'bin/windows-x86_64').mkdir(parents=True)
    (root / 'bin/windows-x86_64/dbcheck.exe').write_bytes(b'fixture binary')
    (root / 'private-state.txt').write_text('excluded fixture')
    release.stage_upload(root, 'component', ['bin/windows-x86_64'], github)
    with zipfile.ZipFile(io.BytesIO(github.content['component.zip'])) as archive:
        assert archive.namelist() == ['bin/windows-x86_64/dbcheck.exe']
    def download(client, asset, destination):
        destination.write_bytes(github.content[asset['name']])
    with patch.object(release, 'download_staged_asset', side_effect=download):
        output = tmp_path / 'restored'
        release.stage_download(output, 'component', github)
    assert (output / 'bin/windows-x86_64/dbcheck.exe').read_bytes() == b'fixture binary'
    assert not (output / 'private-state.txt').exists()


def test_missing_staging_and_archive_path_escape_fail_closed(tmp_path, staging_environment):
    github = StagingGitHub()
    with pytest.raises(ValueError, match='missing'):
        release.stage_download(tmp_path / 'missing', 'component', github)
    assert github.releases == []
    release.staging_release(github, create=True)
    github.assets.append({'id': 1, 'name': 'component.zip', 'state': 'uploaded'})
    def malicious_download(client, asset, path):
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('../escape.txt', 'fixture')
    with patch.object(release, 'download_staged_asset', side_effect=malicious_download):
        with pytest.raises(ValueError, match='Unsafe'):
            release.stage_download(tmp_path / 'output', 'component', github)
    assert not (tmp_path / 'escape.txt').exists()


def test_staging_redirect_never_forwards_github_credentials(tmp_path):
    import hashlib
    import io
    import urllib.error
    from unittest.mock import Mock
    github = StagingGitHub()
    content = b'verified fixture payload'
    asset = {'id': 1, 'size': len(content), 'digest': 'sha256:' + hashlib.sha256(content).hexdigest()}
    opener = Mock()
    opener.open.side_effect = urllib.error.HTTPError('https://api.github.com/fixture', 302, 'redirect',
        {'Location': 'https://release-assets.githubusercontent.com/fixture'}, None)
    with patch.object(release.urllib.request, 'build_opener', return_value=opener), \
         patch.object(release.urllib.request, 'urlopen', return_value=io.BytesIO(content)) as download:
        release.download_staged_asset(github, asset, tmp_path / 'download.zip')
    assert download.call_args.args[0].get_header('Authorization') is None
    assert (tmp_path / 'download.zip').read_bytes() == content


def test_staging_download_rejects_changed_bytes(tmp_path):
    import hashlib
    import io
    from unittest.mock import Mock
    content = b'correct'
    opener = Mock()
    opener.open.return_value = io.BytesIO(b'changed')
    asset = {'id': 1, 'size': len(content), 'digest': 'sha256:' + hashlib.sha256(content).hexdigest()}
    with patch.object(release.urllib.request, 'build_opener', return_value=opener):
        with pytest.raises(ValueError, match='mismatch'):
            release.download_staged_asset(StagingGitHub(), asset, tmp_path / 'download.zip')


def test_release_transport_avoids_artifact_quota_and_preserves_publication_gates():
    import yaml
    producer = (ROOT / '.github/workflows/portable-release.yml').read_text()
    consumer = (ROOT / '.github/workflows/build-release.yml').read_text()
    assert 'actions/upload-artifact@' not in producer + consumer
    assert 'actions/download-artifact@' not in producer + consumer
    jobs = yaml.safe_load(consumer)['jobs']
    assert jobs['publish']['needs'] == ['checks', 'version', 'packages']
    assert jobs['packages']['permissions']['contents'] == 'write'
    native = yaml.safe_load(producer)['jobs']
    assert native['native']['needs'] == 'prepare'
    assert native['verify-transfer']['needs'] == ['assemble', 'windows-state']
    assert 'stage-cleanup' in jobs['publish']['steps'][-2]['run']


def test_staging_cleanup_waits_for_a_complete_published_release(staging_environment):
    github = StagingGitHub()
    stage = release.staging_release(github, create=True)
    published = ArtifactCleanupGitHub().release
    published['id'] = 80
    github.releases.append(published)
    asset = published['assets'].pop()
    with pytest.raises(ValueError, match='incomplete'):
        release.stage_cleanup(published['tag_name'], 'a' * 40, github)
    assert github.deleted == []
    published['assets'].append(asset)
    release.stage_cleanup(published['tag_name'], 'a' * 40, github)
    assert github.deleted == ['releases/' + str(stage['id'])]


def test_windows_package_ships_dameng_native_driver():
    spec = (ROOT / 'build' / 'dbcheck_windows.spec').read_text(encoding='utf-8')
    lock = (ROOT / 'build' / 'portable' / 'requirements-lock.txt').read_text(encoding='utf-8')
    excludes = next(line for line in spec.splitlines() if 'excludes=' in line)
    assert 'dmPython' not in excludes
    assert "'dmPython'" in spec and "'dmssl'" in spec
    assert 'runtime-hook-dmpython.py' in spec
    assert 'libcrypto-3-x64.dll' in spec
    assert 'dmpython==' in lock
    assert 'sys_platform == "win32"' in lock and 'sys_platform == "linux"' in lock
    package_check = (ROOT / 'modules' / 'core' / 'package_check.py').read_text(encoding='utf-8')
    assert 'dmPython' in package_check and 'dmssl' in package_check


def test_linux_package_ships_dameng_native_driver():
    spec = (ROOT / 'build' / 'dbcheck_linux.spec').read_text(encoding='utf-8')
    lock = (ROOT / 'build' / 'portable' / 'requirements-lock.txt').read_text(encoding='utf-8')
    excludes = next(line for line in spec.splitlines() if 'excludes=' in line)
    assert 'dmPython' not in excludes
    assert "'dmPython'" in spec
    # 冻结时必须保留 wheel 的目录布局，否则 $ORIGIN 的 RPATH 解析不到 libdmdpi
    assert "'dmpython.libs'" in spec and "'dmssl'" in spec
    assert 'dmpython==' in lock and 'sys_platform == "linux"' in lock
    package_check = (ROOT / 'modules' / 'core' / 'package_check.py').read_text(encoding='utf-8')
    assert 'startswith("linux")' in package_check
