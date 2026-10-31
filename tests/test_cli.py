import json
import stat
from pathlib import Path

import pytest

from aioecam.__main__ import _save_private


def test_saved_config_is_private(tmp_path: Path) -> None:
    target = tmp_path / "cfg" / "aioecam.json"
    target.parent.mkdir()
    target.write_text("{}")
    target.chmod(0o644)  # a pre-existing, too open file gets tightened
    _save_private(target, {"lan_key": "secret"})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text()) == {"lan_key": "secret"}


def test_cloud_mode_needs_no_lan_key(tmp_path: Path) -> None:
    from aioecam.__main__ import _config

    file = tmp_path / "aioecam.json"
    file.write_text('{"dsn": "AC000W000000002", "refresh_token": "r"}')
    assert _config(file, need_lan_key=False)["dsn"] == "AC000W000000002"
    with pytest.raises(SystemExit):
        _config(file)
