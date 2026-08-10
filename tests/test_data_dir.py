"""Where the database lives, and the rename that came with the app's name.

The folder was called `lol-local-history` before the app was named LoLcal
History. Getting this wrong does not raise — it silently starts an empty history
next to the real one, which looks exactly like losing every game on record.
"""

from __future__ import annotations

import pytest

from lolhist import config


@pytest.fixture
def appdata(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_APPDATA_ROOT", tmp_path)
    monkeypatch.setattr(config, "_LEGACY_APPDATA_DIR", tmp_path / "lol-local-history")
    monkeypatch.delenv("LOLHIST_DATA", raising=False)
    return tmp_path


class TestDataDirResolution:
    def test_fresh_install_uses_the_app_name(self, appdata):
        assert config._resolve_data_dir() == appdata / config.APP_NAME

    def test_an_old_folder_is_moved_across(self, appdata):
        legacy = appdata / "lol-local-history"
        legacy.mkdir()
        (legacy / "history.db").write_text("your games", encoding="utf-8")

        resolved = config._resolve_data_dir()

        assert resolved == appdata / config.APP_NAME
        assert (resolved / "history.db").read_text(encoding="utf-8") == "your games"
        assert not legacy.exists()

    def test_an_existing_new_folder_is_left_alone(self, appdata):
        """Never merge two histories, and never overwrite the current one."""
        legacy = appdata / "lol-local-history"
        legacy.mkdir()
        (legacy / "history.db").write_text("old", encoding="utf-8")
        current = appdata / config.APP_NAME
        current.mkdir()
        (current / "history.db").write_text("current", encoding="utf-8")

        resolved = config._resolve_data_dir()

        assert resolved == current
        assert (current / "history.db").read_text(encoding="utf-8") == "current"
        assert legacy.exists(), "the old folder is left for the user to deal with"

    def test_a_failed_move_keeps_using_the_old_folder(self, appdata, monkeypatch):
        """Better to carry on from where the data is than to start empty."""
        legacy = appdata / "lol-local-history"
        legacy.mkdir()
        (legacy / "history.db").write_text("your games", encoding="utf-8")

        def refuse(self, target):
            raise OSError("in use")

        monkeypatch.setattr(config.Path, "rename", refuse)

        resolved = config._resolve_data_dir()
        assert resolved == legacy
        assert (resolved / "history.db").read_text(encoding="utf-8") == "your games"

    def test_the_env_override_still_wins(self, appdata, monkeypatch):
        legacy = appdata / "lol-local-history"
        legacy.mkdir()
        monkeypatch.setenv("LOLHIST_DATA", str(appdata / "elsewhere"))

        assert config._resolve_data_dir() == appdata / "elsewhere"
        assert legacy.exists(), "an override must not trigger the move"
