"""The update check, and the release pipeline it depends on.

This is the only part of the tool that talks to the internet, so it gets the
most suspicious tests in the suite. Nothing here touches the network: the
GitHub response is a canned payload and the installer is never actually run.

The rules being defended:

* an unverified download must never be executed;
* the version the app reports must equal the tag it shipped under, or the
  comparison that drives the whole feature is meaningless;
* the check must fail quietly — offline, rate-limited or 404, it is never
  allowed to break the dashboard.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from lolhist import config, updates, version
from lolhist.web import app as web_app

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
ISS = REPO_ROOT / "installer" / "lolcal-history.iss"


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """Each test starts with no cached check and the feature enabled."""
    monkeypatch.setattr(updates, "_state", updates.State())
    monkeypatch.delenv("LOLHIST_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(updates, "_quit_hook", None)


def a_release(tag="v9.9.9", *, installer=True, checksums=True) -> dict:
    assets = []
    if installer:
        assets.append({
            "name": f"LoLcal-History-{tag.lstrip('v')}-Setup.exe",
            "browser_download_url": f"https://example.invalid/{tag}/Setup.exe",
            "size": 12345,
        })
    if checksums:
        assets.append({
            "name": "SHA256SUMS.txt",
            "browser_download_url": f"https://example.invalid/{tag}/SHA256SUMS.txt",
            "size": 100,
        })
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/KioCoan/LoLcalHistory/releases/tag/{tag}",
        "body": "notes",
        "published_at": "2026-08-11T00:00:00Z",
        "assets": assets,
    }


class TestVersionComparison:
    @pytest.mark.parametrize("latest,current,expected", [
        ("0.2.0", "0.1.0", True),
        ("v0.2.0", "0.1.0", True),
        ("0.1.1", "0.1.0", True),
        ("1.0.0", "0.9.9", True),
        ("0.1.0", "0.1.0", False),
        ("0.1.0", "0.2.0", False),
        ("0.10.0", "0.9.0", True),   # not a string comparison
    ])
    def test_ordering(self, latest, current, expected):
        assert version.is_newer(latest, current) is expected

    def test_a_shorter_version_is_padded_not_ranked_by_length(self):
        assert version.is_newer("1.2", "1.2.0") is False
        assert version.is_newer("1.2.0", "1.2") is False

    def test_a_prerelease_does_not_nag_the_person_running_it(self):
        assert version.is_newer("1.2.0", "1.2.0-rc1") is False

    def test_garbage_never_triggers_an_update(self):
        assert version.parse("not-a-version") == ()
        assert version.is_newer("not-a-version", "0.1.0") is False
        assert version.is_newer("9.9.9", "not-a-version") is False


class TestChecking:
    def test_a_newer_release_is_offered(self, monkeypatch):
        monkeypatch.setattr(updates.httpx, "get", lambda *a, **k: _ok(a_release()))
        state = updates.check(force=True)

        assert state["available"] is True
        assert state["latest"] == "9.9.9"
        assert state["release"]["installer_url"].endswith("Setup.exe")
        assert state["release"]["checksums_url"].endswith("SHA256SUMS.txt")

    def test_the_current_version_is_not_an_update(self, monkeypatch):
        payload = a_release(tag=f"v{version.__version__}")
        monkeypatch.setattr(updates.httpx, "get", lambda *a, **k: _ok(payload))
        assert updates.check(force=True)["available"] is False

    def test_being_offline_is_not_an_error(self, monkeypatch):
        def explode(*a, **k):
            raise updates.httpx.ConnectError("no route to host")

        monkeypatch.setattr(updates.httpx, "get", explode)
        state = updates.check(force=True)

        assert state["available"] is False
        assert state["checked_at"] > 0, "a failed check must still back off"

    def test_no_published_release_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(updates.httpx, "get", lambda *a, **k: _ok({}, status=404))
        assert updates.check(force=True)["available"] is False

    def test_the_result_is_cached_between_checks(self, monkeypatch):
        calls = []

        def counted(*a, **k):
            calls.append(1)
            return _ok(a_release())

        monkeypatch.setattr(updates.httpx, "get", counted)
        updates.check(force=True)
        updates.check()
        updates.check()

        assert len(calls) == 1, "GitHub was asked again inside the cache window"

    def test_the_cache_survives_a_restart(self, monkeypatch):
        monkeypatch.setattr(updates.httpx, "get", lambda *a, **k: _ok(a_release()))
        updates.check(force=True)

        monkeypatch.setattr(updates, "_state", updates.State())
        assert updates.state()["latest"] == "9.9.9"

    def test_disabling_it_stops_every_request(self, monkeypatch):
        def explode(*a, **k):
            raise AssertionError("asked GitHub with the check disabled")

        monkeypatch.setenv("LOLHIST_NO_UPDATE_CHECK", "1")
        monkeypatch.setattr(updates.httpx, "get", explode)

        state = updates.check(force=True)
        assert state["enabled"] is False
        assert state["available"] is False


class TestInstallSafety:
    """An installer runs with the user's own privileges. It gets verified."""

    @pytest.fixture
    def staged(self, monkeypatch):
        """A downloaded installer plus whatever checksum listing a test wants."""
        payload = b"pretend this is Setup.exe"
        digest = hashlib.sha256(payload).hexdigest()
        launched = []
        monkeypatch.setattr(updates, "_launch", lambda path: launched.append(path))
        monkeypatch.setattr(config, "FROZEN", True)

        def stage(listing: str):
            monkeypatch.setattr(
                updates, "_download",
                lambda client, url, target, on_progress: target.write_bytes(payload),
            )

            class Client:
                def __init__(self, *a, **k): pass
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def get(self, url): return _text(listing)

            monkeypatch.setattr(updates.httpx, "Client", Client)
            release = updates._parse_release(a_release())
            updates.install(release)
            return launched

        stage.digest = digest
        stage.name = "LoLcal-History-9.9.9-Setup.exe"
        return stage

    def test_a_matching_checksum_runs_the_installer(self, staged):
        launched = staged(f"{staged.digest}  {staged.name}\n")
        assert len(launched) == 1
        assert updates.state()["status"] == "installing"

    def test_a_wrong_checksum_is_never_executed(self, staged):
        launched = staged(f"{'0' * 64}  {staged.name}\n")

        assert launched == [], "ran an installer that failed verification"
        state = updates.state()
        assert state["status"] == "failed"
        assert "checksum" in state["error"]

    def test_a_missing_checksum_entry_is_never_executed(self, staged):
        launched = staged("deadbeef  something-else.exe\n")

        assert launched == []
        assert updates.state()["status"] == "failed"

    def test_a_release_without_an_installer_is_refused(self, monkeypatch):
        monkeypatch.setattr(config, "FROZEN", True)
        release = updates._parse_release(a_release(installer=False))
        updates.install(release)

        assert updates.state()["status"] == "failed"

    def test_a_source_checkout_is_told_to_use_git(self, monkeypatch):
        monkeypatch.setattr(config, "FROZEN", False)
        updates.install(updates._parse_release(a_release()))

        state = updates.state()
        assert state["status"] == "failed"
        assert "git" in state["error"]

    def test_a_network_failure_is_phrased_for_a_person(self, monkeypatch):
        """`[Errno 11001] getaddrinfo failed` is not a message for a dashboard."""
        monkeypatch.setattr(config, "FROZEN", True)

        class Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(updates.httpx, "Client", Client)

        def unreachable(*a, **k):
            raise updates.httpx.ConnectError("[Errno 11001] getaddrinfo failed")

        monkeypatch.setattr(updates, "_download", unreachable)
        updates.install(updates._parse_release(a_release()))

        error = updates.state()["error"]
        assert "internet connection" in error
        assert "Errno" not in error

    def test_an_unrecognised_failure_is_passed_through_not_hidden(self):
        assert updates._readable(ValueError("checksum mismatch")) == "checksum mismatch"

    def test_the_installer_waits_for_the_app_to_exit(self, monkeypatch):
        """The crash this was written for.

        Setup used to start straight away, so its Restart Manager found the app
        mid-shutdown and force-killed the one-file bootloader. That took the
        unpacked %TEMP%\\_MEIxxxx directory with it while the child process was
        still alive, which then died on its next import:

            Failed to load Python DLL '...\\_MEI77522\\python313.dll'
        """
        spawned = {}

        def capture(argv, **kwargs):
            spawned["argv"] = argv
            spawned["kwargs"] = kwargs

        monkeypatch.setattr(updates.subprocess, "Popen", capture)
        updates._launch(Path(r"C:\some path\Setup.exe"))

        script = spawned["argv"][-1]
        assert "Get-Process" in script, "Setup was started without waiting"
        # Waits on the process name, so both the bootloader and the child count.
        assert config.APP_NAME in script
        assert "Start-Process" in script
        # The wait must come first, or it is not a wait.
        assert script.index("Get-Process") < script.index("Start-Process")

    def test_the_waiter_is_not_spawned_detached(self, monkeypatch):
        """DETACHED_PROCESS looks right and is a silent no-op.

        With no console at all, PowerShell exits immediately without running
        the script, while Popen still reports success — so the update simply
        never happened and nothing anywhere said so.
        """
        detached = getattr(updates.subprocess, "DETACHED_PROCESS", 0)
        if detached:
            assert not (updates._INDEPENDENT & detached)

        spawned = {}
        monkeypatch.setattr(
            updates.subprocess, "Popen",
            lambda argv, **kw: spawned.update(kw),
        )
        updates._launch(Path(r"C:\x\Setup.exe"))
        assert spawned["creationflags"] == updates._INDEPENDENT

    def test_the_waiter_runs_hidden(self):
        if hasattr(updates.subprocess, "CREATE_NO_WINDOW"):
            assert updates._INDEPENDENT & updates.subprocess.CREATE_NO_WINDOW

    def test_a_path_with_a_quote_cannot_break_out_of_the_script(self, monkeypatch):
        """The installer path is composed into a PowerShell string."""
        spawned = {}
        monkeypatch.setattr(
            updates.subprocess, "Popen",
            lambda argv, **kw: spawned.update(argv=argv),
        )
        updates._launch(Path(r"C:\odd'name\Setup.exe"))

        script = spawned["argv"][-1]
        assert "odd''name" in script, "a single quote was not escaped"

    def test_the_digest_parser_ignores_other_entries(self):
        listing = "aaa  one.exe\nbbb  two.exe\nccc  LoLcal-History-1.0.0-Setup.exe\n"
        assert updates._expected_digest(listing, "LoLcal-History-1.0.0-Setup.exe") == "ccc"
        assert updates._expected_digest(listing, "missing.exe") is None

    def test_binary_marked_entries_are_understood(self):
        """`sha256sum -b` writes the name with a leading asterisk."""
        assert updates._expected_digest("abc *setup.exe\n", "setup.exe") == "abc"


class TestDashboard:
    @pytest.fixture
    def client(self):
        return web_app.create_app().test_client()

    def test_the_endpoint_reports_the_running_version(self, client):
        body = client.get("/api/update").get_json()
        assert body["current"] == version.__version__
        assert body["available"] is False

    def test_show_is_refused_when_there_is_no_window(self, client):
        assert client.post("/api/show").status_code == 409

    def test_show_raises_the_window_when_there_is_one(self):
        raised = []
        app = web_app.create_app(on_show=lambda: raised.append(1))
        assert app.test_client().post("/api/show").status_code == 200
        assert raised == [1]


class TestReleasePipeline:
    """The workflow and installer are the delivery mechanism; a typo in either
    is only discovered at tag time otherwise."""

    def test_the_workflow_is_valid_yaml_and_triggers_on_tags(self):
        yaml = pytest.importorskip("yaml")
        spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        # `on` is parsed as the boolean True by YAML 1.1.
        triggers = spec.get("on") or spec.get(True)
        assert triggers["push"]["tags"] == ["v*"]
        assert spec["permissions"]["contents"] == "write"

    def test_the_workflow_builds_on_windows_and_runs_the_tests(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "runs-on: windows-latest" in text
        assert "pytest" in text
        assert "innosetup" in text

    def test_the_installer_can_close_an_app_that_refuses_to_close(self):
        """The regression this was written for.

        The app hides to the tray instead of quitting, so it ignores the
        WM_CLOSE that Restart Manager sends. With plain `CloseApplications=yes`
        Setup reported "unable to automatically close all applications", and
        under /SUPPRESSMSGBOXES that became a silent cancel and rollback — the
        installer appeared to succeed while replacing nothing.
        """
        text = ISS.read_text(encoding="utf-8")
        assert "CloseApplications=force" in text

    def test_the_installer_does_not_wait_on_the_app_mutex(self):
        """Setup is launched by the app that still holds it, so an AppMutex
        would abort every silent update before it started."""
        directives = [
            line for line in ISS.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("AppMutex")
        ]
        assert not directives, f"AppMutex would deadlock the update path: {directives}"

    def test_the_installer_needs_no_administrator(self):
        """A UAC prompt would stall the silent update with nobody to answer it."""
        text = ISS.read_text(encoding="utf-8")
        assert "PrivilegesRequired=lowest" in text
        assert "{localappdata}\\Programs" in text

    def test_the_installer_relaunches_after_a_silent_install(self):
        """Without this the update button would leave the app closed."""
        section = ISS.read_text(encoding="utf-8").split("\n[Run]\n")[1]
        # Directives only — a `;` comment explaining the flags is not one.
        directives = [
            line for line in section.splitlines()
            if line.strip().startswith("Filename:")
        ]

        assert directives, "nothing runs after the install"
        assert all("skipifsilent" not in line for line in directives), (
            "a silent install would not relaunch the app"
        )
        assert any("nowait" in line for line in directives)

    def test_the_asset_name_the_updater_looks_for_is_the_one_built(self):
        """The updater matches assets ending in `setup.exe`; the workflow has to
        publish one that does."""
        iss = ISS.read_text(encoding="utf-8")
        assert "OutputBaseFilename=LoLcal-History-{#AppVersion}-Setup" in iss

        built = "LoLcal-History-9.9.9-Setup.exe"
        assert built.lower().endswith("setup.exe")
        assert updates.CHECKSUM_ASSET in WORKFLOW.read_text(encoding="utf-8")

    def test_the_project_declares_how_to_build_itself(self):
        """The first release build died here.

        CI installs the project to get its extras, but the package had no
        `[build-system]` and no explicit package list. setuptools' flat-layout
        discovery then found `lolhist` next to `tests`, `tools`, `installer` and
        `data`, refused to guess, and failed before a single test ran. Nothing
        locally exercised `pip install -e .`, so it went unnoticed.
        """
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "[build-system]" in pyproject
        assert "build-backend" in pyproject
        assert '"lolhist"' in pyproject, "packages must be listed, not discovered"

    def test_the_workflow_installs_what_the_tests_import(self):
        """A dependency missing from the dev extras makes its test skip on CI,
        which looks identical to passing."""
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "pyyaml" in pyproject.lower()

    def test_a_labelled_version_does_not_break_the_file_version(self):
        """Windows' version resource takes digits and dots only, so Inno aborts
        on "0.0.0-test" — which was the rehearsal button's default input."""
        iss = ISS.read_text(encoding="utf-8")
        assert "VersionInfoVersion={#NumericVersion}" in iss

        workflow = WORKFLOW.read_text(encoding="utf-8")
        assert "DNumericVersion" in workflow
        assert "numeric=" in workflow

    def test_the_declared_version_matches_pyproject(self):
        """The workflow checks the tag against version.py; keep pyproject with
        it so the package and the app never disagree."""
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        declared = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
        assert declared == version.__version__


def _ok(payload: dict, status: int = 200):
    class Response:
        status_code = status
        text = json.dumps(payload)

        def json(self): return payload

        def raise_for_status(self):
            if status >= 400:
                raise updates.httpx.HTTPStatusError("boom", request=None, response=None)

    return Response()


def _text(body: str):
    class Response:
        status_code = 200
        text = body

        def raise_for_status(self): return None

    return Response()
