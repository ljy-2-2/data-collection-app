"""Qt must keep its 3D renderer and respect recipient desktop scaling."""
import os
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import desktop_app

@pytest.fixture(autouse=True)
def renderer_env(monkeypatch):
    for key in ('APP_SOFTWARE_RENDERING', 'LIBGL_ALWAYS_SOFTWARE', 'QSG_RHI_BACKEND', 'QT_SCALE_FACTOR', 'APP_UI_SCALE'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(desktop_app, 'desktop_text_scale', lambda: 1.25)

def test_normal_mode_respects_desktop_scale_and_full_3d():
    assert not desktop_app.configure_rendering({})
    assert os.environ['QSG_RHI_BACKEND']=='opengl'
    assert os.environ['QT_SCALE_FACTOR']=='1.25'
    assert 'LIBGL_ALWAYS_SOFTWARE' not in os.environ

@pytest.mark.parametrize('cli,config', [(True, {}), (False, {'APP_SOFTWARE_RENDERING':'1'})])
def test_software_mode_keeps_3d_via_mesa(cli,config):
    assert desktop_app.configure_rendering(config,software=cli)
    assert os.environ['LIBGL_ALWAYS_SOFTWARE']=='1'
    assert os.environ['QSG_RHI_BACKEND']=='opengl'

def test_user_scale_override(monkeypatch):
    monkeypatch.setenv('QT_SCALE_FACTOR','1.5')
    desktop_app.configure_rendering({'APP_UI_SCALE':'2'})
    assert os.environ['QT_SCALE_FACTOR']=='1.5'

@pytest.mark.parametrize('value',['NaN','inf','garbage','-1','0'])
def test_invalid_scale_does_not_break_window(value):
    desktop_app.configure_rendering({'APP_UI_SCALE':value})
    assert os.environ['QT_SCALE_FACTOR']=='1.25'
