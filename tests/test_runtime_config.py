from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from generate_runtime_config import browser_config, platform_urls, read_env  # noqa: E402


def test_read_env_preserves_bcrypt_dollar_signs(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("AUTH_PASSWORD_HASH='$2b$12$abc'\nPLATFORM_API_URL=https://api.example.com\n")
    values = read_env(env_file)
    assert values["AUTH_PASSWORD_HASH"] == "$2b$12$abc"
    assert values["PLATFORM_API_URL"] == "https://api.example.com"


def test_browser_config_exposes_no_auth_secrets():
    result = browser_config({
        "PLATFORM_API_URL": "https://api.example.com",
        "PLATFORM_WEB_URL": "https://platform.example.com",
        "AUTH_SECRET_KEY": "never-send-this",
        "AUTH_PASSWORD_HASH": "never-send-this-either",
    })
    assert result["platformApiUrl"] == "https://api.example.com"
    assert "AUTH_SECRET_KEY" not in result
    assert "AUTH_PASSWORD_HASH" not in result


@pytest.mark.parametrize('api,web', [
    ('https://api.example.com', 'http://127.0.0.1:5173'),
    ('http://localhost:8000', 'https://platform.example.com'),
])
def test_partial_switch_cannot_mix_cloud_and_local(api, web):
    with pytest.raises(ValueError, match='同时设置'):
        platform_urls({'PLATFORM_API_URL': api, 'PLATFORM_WEB_URL': web})


def test_switch_updates_both_addresses_preserves_other_config(tmp_path):
    from configure_platform import configure
    path = tmp_path / '.env'
    path.write_text('# settings\nPLATFORM_API_URL=http://127.0.0.1:8000\n'
                    'PLATFORM_WEB_URL=http://127.0.0.1:5173\nAUTH_SECRET=unchanged\n')
    configure(path, 'https://api.example.com/api', 'https://platform.example.com')
    values = read_env(path)
    assert values['AUTH_SECRET'] == 'unchanged'
    assert browser_config(values)['platformApiUrl'] == 'https://api.example.com/api'
    assert browser_config(values)['platformWebUrl'] == 'https://platform.example.com'
    assert path.read_text().startswith('# settings\n')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        configure(path, 'http://127.0.0.1:8000', 'https://platform.example.com')
    assert path.read_bytes() == before
    configure(path, 'http://127.0.0.1:8000', 'http://127.0.0.1:5173')
    assert platform_urls(read_env(path)) == platform_urls({})


def test_installed_workspace_updates_persistent_config(tmp_path):
    from configure_platform import configure
    persistent = tmp_path / 'user.env'
    persistent.write_text('APP_PORT=4173\n')
    workspace = tmp_path / '.env'
    workspace.symlink_to(persistent)
    configure(workspace, 'https://api.example.com', 'https://platform.example.com')
    assert workspace.is_symlink()
    assert read_env(persistent)['PLATFORM_WEB_URL'] == 'https://platform.example.com'
    assert read_env(persistent)['APP_PORT'] == '4173'
