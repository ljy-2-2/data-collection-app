"""Exercise standalone smoke assertions without Docker or host installation."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import smoke_self_contained as smoke

CHECKS = {}
exec(smoke.CHECKS, CHECKS)


@pytest.mark.parametrize('ubuntu', [None, '22.04', '24.04'])
def test_smoke_installs_only_inside_an_offline_isolated_container(tmp_path, monkeypatch, ubuntu):
    deb = tmp_path / 'new app.deb'
    deb.touch()
    argv = ['smoke_self_contained.py', str(deb)]
    if ubuntu:
        argv += ['--ubuntu', ubuntu]
    monkeypatch.setattr(sys, 'argv', argv)
    calls = []
    monkeypatch.setattr(smoke.subprocess, 'run', lambda command, **kwargs:
                        calls.append((command, kwargs)) or SimpleNamespace(returncode=0))
    smoke.main()
    assert len(calls) == 1
    command, options = calls[0]
    assert command[:2] == ['docker', 'run']
    assert '--pull=never' in command
    assert command[command.index('--network') + 1] == 'none'
    assert command[-3:] == ['ubuntu:' + (ubuntu or '22.04'), 'bash', '-s']
    assert command.count('--mount') == 1
    assert command[command.index('--mount') + 1] == f'type=bind,source={deb.resolve()},target=/input/app.deb,readonly'
    assert not any(flag in command for flag in ('--privileged', '--device', '--volume', '-v', '--pid', '--ipc'))
    assert 'dpkg -i /input/app.deb' in options['input']
    assert 'useradd -m -u 2000 xiaokai-test' in options['input']
    assert 'SUDO_UID=2000' in options['input']
    assert smoke.GROUP_VERIFY in options['input']
    assert smoke.ROBOT_VERIFY in options['input']
    assert smoke.ROBOT_DRIVER in options['input']
    assert 'verify-first-install-groups.py' in options['input']
    assert options['input'].index('/tmp/verify-first-install-groups.py') < options['input'].index('/tmp/verify-robot-runtime.py')
    assert smoke.VERIFY in options['input']
    assert options['check'] is True


def test_unknown_ubuntu_version_does_not_launch_docker(tmp_path, monkeypatch):
    deb = tmp_path / 'app.deb'
    deb.touch()
    monkeypatch.setattr(sys, 'argv', ['smoke', str(deb), '--ubuntu', '20.04'])
    monkeypatch.setattr(smoke.subprocess, 'run', lambda *a, **k: pytest.fail('must not launch'))
    with pytest.raises(SystemExit):
        smoke.main()


@pytest.mark.parametrize('relative', [
    'defaults.env', 'runtime/env.sh',
    'runtime/python/lib/python3.12/site-packages/checkout.pth',
    'app/umi_collection/vendor/galaxis/.env',
    'app/teleop_control/vendor/tianji/teleop/gripper/config.json',
    'runtime/robot/bin/python3',
    'runtime/robot/xr/runService.sh',
    'runtime/robot/gdk/gdk/config/app_conf.json',
])
def test_rejects_configuration_that_loads_from_builder_home(tmp_path, relative):
    config = tmp_path / relative
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('PYTHONPATH=/home/ljy/桌面/galaxis\n')
    with pytest.raises(AssertionError, match='build machine'):
        CHECKS['check_configuration_paths'](tmp_path)


def test_build_paths_in_docs_are_not_treated_as_runtime_dependencies(tmp_path):
    (tmp_path / 'notes.md').write_text('Example: /home/ljy/old-build')
    CHECKS['check_configuration_paths'](tmp_path)
    (tmp_path / 'linked-runtime').symlink_to('/home/ljy/private/runtime')
    with pytest.raises(AssertionError, match='symlink'):
        CHECKS['check_configuration_paths'](tmp_path)


def test_actual_user_workspace_configuration_is_checked(tmp_path):
    package, workspace = tmp_path / 'package', tmp_path / 'new-user'
    package.mkdir(); workspace.mkdir()
    (workspace / '.env').write_text('GDK_PYTHON=/home/ljy/env/bin/python')
    with pytest.raises(AssertionError, match='build machine'):
        CHECKS['check_configuration_paths'](package, workspace)


def test_private_tool_must_be_executable_and_stay_in_the_package(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    tool = runtime / 'v4l2-ctl'
    tool.write_text('#!/bin/sh\nexit 0\n')
    with pytest.raises(AssertionError, match='missing'):
        CHECKS['check_private_executable'](tool, tmp_path)
    tool.chmod(0o755)
    CHECKS['check_private_executable'](tool, tmp_path)
    tool.unlink()
    tool.symlink_to(sys.executable)
    with pytest.raises(AssertionError, match='escaped'):
        CHECKS['check_private_executable'](tool, tmp_path)


def test_robot_dependencies_must_be_loaded_from_the_packaged_runtime(tmp_path):
    package=tmp_path/'package'
    module_path=package/'runtime/robot/python-site/numpy/__init__.py'
    module_path.parent.mkdir(parents=True);module_path.touch()
    module=SimpleNamespace(__name__='numpy',__file__=str(module_path))
    CHECKS['check_private_module'](module,package)
    module.__file__=str(tmp_path/'host-site/numpy/__init__.py')
    with pytest.raises(AssertionError,match='escaped private runtime'):
        CHECKS['check_private_module'](module,package)
    module.__file__=None
    with pytest.raises(AssertionError,match='Cannot locate'):
        CHECKS['check_private_module'](module,package)


def test_both_brightness_routes_are_required():
    path = '/umi/camera-controls/{camera_key}/brightness'
    for route in ({}, {'get': {}}, {'post': {}}):
        with pytest.raises(AssertionError, match='GET/POST'):
            CHECKS['check_brightness_routes']({'paths': {path: route}})
    CHECKS['check_brightness_routes']({'paths': {path: {'get': {}, 'post': {}}}})


@pytest.mark.parametrize('output', [
    'dlopen for libdynamic_aorta.so fail, error: file not found',
    'libcamera.so: cannot open shared object file: No such file or directory',
    'error while loading shared libraries: libmissing.so',
    'ImportError: module.so: undefined symbol: missing_api',
    "libstdc++.so: version `GLIBCXX_3.4.99' not found",
])
def test_native_loader_failures_cannot_hide_behind_successful_sdk_import(output):
    with pytest.raises(AssertionError,match='native dependency'):
        CHECKS['check_native_dependency_log'](output)


def test_device_absence_and_discovery_messages_do_not_fail_isolated_sdk_import():
    CHECKS['check_native_dependency_log']('Initialising DDS\nNo camera found\nCannot open camera config file\nFailed to connect to robot\nDiscovery timeout\n')


def test_embedded_installed_runtime_verifier_compiles():
    compile(smoke.VERIFY, '<installed-runtime-verifier>', 'exec')
    compile(smoke.ROBOT_VERIFY, '<robot-runtime-verifier>', 'exec')
    compile(smoke.ROBOT_DRIVER, '<robot-driver-verifier>', 'exec')
    compile(smoke.APP_DEFAULTS, '<app-defaults-verifier>', 'exec')


def test_app_defaults_are_checked_in_package_and_actual_user_workspace():
    assert "APP_DEFAULTS,str(package/'app')" in smoke.VERIFY
    assert 'APP_DEFAULTS,str(workspace)' in smoke.VERIFY
    assert 'args=control.parse_args()' in smoke.APP_DEFAULTS
    assert "robot_configuration()['service_dir']" in smoke.APP_DEFAULTS
    assert "env['APP_CONF_PATH']" in smoke.APP_DEFAULTS


def test_robot_smoke_uses_memory_only_models_and_never_initializes_device_sdks():
    import ast

    tree=ast.parse(smoke.ROBOT_VERIFY)
    called={ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node,ast.Call)}
    assert 'solver.solve' in called
    assert 'arm.fk' in called
    assert 'Models' in called
    assert 'native_robot.connect' not in called
    assert 'xrobotoolkit_sdk.init' not in called
    assert 'agibot_gdk.init' not in called
    assert 'process_listeners' in called
    assert "'app/scripts/smoke_qt_ui.py'" in smoke.VERIFY


def test_robot_smoke_imports_real_production_workers_and_collection_modules():
    import ast

    tree=ast.parse(smoke.ROBOT_VERIFY)
    selected=next(ast.literal_eval(node.value) for node in tree.body
                  if isinstance(node,ast.Assign) and any(
                      isinstance(target,ast.Name) and target.id=='worker_modules'
                      for target in node.targets))
    assert {'teleop_control.tianji_worker','teleop_control.gdk_preview_worker',
            'teleop_control.gdk_move','run_new_teleop_collection','remote_collector',
            'apps.pico_g2_dual_arm_live','collector.collector','collector.episode_writer'} <= set(selected)
    called={ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node,ast.Call)}
    assert not any(name.endswith('.main') for name in called)


def test_first_install_group_probe_compiles_and_uses_real_launcher_and_proc_scanner():
    compile(smoke.GROUP_PROBE, '<first-install-group-probe>', 'exec')
    compile('GROUP_PROBE = ' + repr(smoke.GROUP_PROBE) + '\n' + smoke.GROUP_VERIFY,
            '<first-install-group-verifier>', 'exec')
    assert 'package_launcher.ensure_collection_permissions(fixture)' in smoke.GROUP_PROBE
    assert 'collector.legacy_robotics_service_pids()' in smoke.GROUP_PROBE
    assert 'collector.terminate_legacy_robotics_service()==[legacy_pid]' in smoke.GROUP_PROBE
    assert "listener.bind(('127.0.0.1',63901))" in smoke.GROUP_PROBE
    assert "'--supp-group',user.pw_name" in smoke.GROUP_VERIFY
    assert 'os.setgroups([user.pw_gid])' in smoke.GROUP_VERIFY
    assert 'service.poll() is None' in smoke.GROUP_VERIFY
