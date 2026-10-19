"""Scope RK3588 Mali compatibility to the desktop process, not its backend."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def mali_preloads(root=ROOT):
    try:
        if b'rockchip,rk3588' not in Path('/sys/firmware/devicetree/base/compatible').read_bytes():
            return []
    except OSError:
        return []
    driver = Path('/usr/lib/aarch64-linux-gnu/libmali.so.1')
    if not driver.is_file():
        return []
    for folder in (root/'.runtime/graphics', root.parent/'runtime/graphics', root/'.build-cache/orangepi-graphics'):
        library = folder/'libxiaokai-mali-x11.so'
        if library.is_file():
            return [str(library.resolve()), str(driver)]
    return []


def prepare_desktop(values, *, software=False):
    if os.environ.get('XIAOKAI_MALI_ACTIVE'):
        return
    software = software or os.environ.get('APP_SOFTWARE_RENDERING', values.get('APP_SOFTWARE_RENDERING','0')).lower() in ('1','true','yes')
    platform = os.environ.get('QT_QPA_PLATFORM', '')
    integration = os.environ.get('QT_XCB_GL_INTEGRATION', '')
    if (software or platform not in ('', 'xcb') or os.environ.get('WAYLAND_DISPLAY')
            or integration not in ('', 'xcb_egl')):
        return
    libraries = mali_preloads()
    if not libraries:
        return
    env = os.environ.copy()
    env['XIAOKAI_MALI_ACTIVE'] = '1'
    env['XIAOKAI_PRELOAD_BEFORE_GRAPHICS'] = env.get('LD_PRELOAD', '')
    env['LD_PRELOAD'] = ':'.join([*libraries, *filter(None, [env.get('LD_PRELOAD','')])])
    env['QT_XCB_GL_INTEGRATION'] = 'xcb_egl'
    os.execve(sys.executable, [sys.executable, *sys.argv], env)


def backend_environment():
    env = os.environ.copy()
    if env.pop('XIAOKAI_MALI_ACTIVE', None):
        original = env.pop('XIAOKAI_PRELOAD_BEFORE_GRAPHICS', '')
        if original:
            env['LD_PRELOAD'] = original
        else:
            env.pop('LD_PRELOAD', None)
        env.pop('QT_XCB_GL_INTEGRATION', None)
    return env
