"""Fake Docker CLI only; no daemon, hardware, or operator data is accessed."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('start_compose_services',
    ROOT / 'umi_collection/vendor/galaxis/tools/start_compose_services.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def result(code=0, out='', error=''):
    return SimpleNamespace(returncode=code, stdout=out, stderr=error)


def record(index, service, config, *, running=False):
    return {'Id': f'{index:064x}', 'Config': {'Labels': {
        'com.docker.compose.project': 'galaxis',
        'com.docker.compose.service': service,
        'com.docker.compose.project.config_files': str(config),
        'com.docker.compose.project.working_dir': str(config.parent),
        'com.docker.compose.oneoff': 'False',
    }}, 'State': {'Status': 'running' if running else 'exited',
                 'Running': running, 'Paused': False, 'Restarting': False, 'Error': ''}}


class Docker:
    def __init__(self, config, services=('config-init', *recovery.SERVICES)):
        self.records = {r['Id']: r for r in (
            record(i+1, service, config) for i, service in enumerate(services))}
        self.broken = set(self.records)
        self.calls = []
        self.initial = result(1, error='failed to load container mount: RW layer abc not found\n')
        self.retry = result()
        self.on_reinspect = None
        self.fail_diff = None

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ['docker', 'compose']:
            command = args[4]
            if command == 'up':
                return self.retry if '--no-recreate' in args else self.initial
            if command == 'config':
                return result(out=json.dumps({'name': 'galaxis'}))
            if command == 'ps':
                assert set(args[7:]) == recovery.OWNED_SERVICES
                return result(out='\n'.join(self.records))
        if args[:2] == ['docker', 'inspect']:
            ids = args[2:]
            if len(ids) == 1 and self.on_reinspect:
                self.on_reinspect(self.records[ids[0]])
            return result(out=json.dumps([copy.deepcopy(self.records[key]) for key in ids]))
        if args[:2] == ['docker', 'diff']:
            if self.fail_diff:
                return self.fail_diff
            return result(1, error='RWLayer is unexpectedly nil for container '+args[2]) if args[2] in self.broken else result()
        if args[:2] == ['docker', 'rm']:
            assert len(args) == 3  # No -f, -v or name based removal.
            assert not self.records[args[2]]['State']['Running']
            del self.records[args[2]]
            return result()
        raise AssertionError(args)

    @property
    def removed(self):
        return [args[-1] for args in self.calls if args[:2] == ['docker', 'rm']]


def test_success_is_original_compose_up_only(tmp_path):
    config = tmp_path/'docker-compose.yml'; docker = Docker(config)
    docker.initial = result()
    assert recovery.start(config, docker) == 0
    assert len(docker.calls) == 1 and '--no-recreate' not in docker.calls[0]


@pytest.mark.parametrize('message', ['permission denied', 'Cannot connect to Docker daemon',
                                    'port is already allocated', 'no space left on device'])
def test_unrelated_error_is_preserved_without_cleanup(tmp_path, capsys, message):
    config = tmp_path/'docker-compose.yml'; docker = Docker(config)
    docker.initial = result(42, error=message)
    assert recovery.start(config, docker) == 42
    assert len(docker.calls) == 1 and not docker.removed
    assert message in capsys.readouterr().err


def test_missing_layers_are_detected_even_before_state_error_populated(tmp_path, capsys):
    config = tmp_path/'docker-compose.yml'; docker = Docker(config)
    expected = set(docker.records)
    assert all(not c['State']['Error'] for c in docker.records.values())
    assert recovery.start(config, docker) == 0
    assert set(docker.removed) == expected
    assert sum(args[:2] == ['docker', 'compose'] and args[4] == 'up' for args in docker.calls) == 2
    assert '--no-recreate' in docker.calls[-1]
    assert 'RW layer abc not found' in capsys.readouterr().err


def test_running_and_healthy_stopped_services_are_never_removed(tmp_path):
    config = tmp_path/'docker-compose.yml'; docker = Docker(config)
    ids = list(docker.records)
    docker.records[ids[0]]['State'].update(Status='running', Running=True)
    docker.broken.remove(ids[1])
    assert recovery.start(config, docker) == 0
    assert set(docker.removed) == set(ids[2:])
    assert ['docker', 'diff', ids[0]] not in docker.calls


@pytest.mark.parametrize('field,value', [
    ('com.docker.compose.project', 'foreign-project'),
    ('com.docker.compose.service', 'foreign-service'),
    ('com.docker.compose.project.config_files', '/foreign/docker-compose.yml'),
    ('com.docker.compose.project.working_dir', '/foreign'),
])
def test_foreign_ownership_refuses_all_removals(tmp_path, field, value):
    config = tmp_path/'docker-compose.yml'; docker = Docker(config)
    list(docker.records.values())[-1]['Config']['Labels'][field] = value
    assert recovery.start(config, docker) == 1
    assert not docker.removed


def test_same_app_workspace_migration_uses_selected_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    suffix = 'umi_collection/vendor/galaxis/docker-compose.yml'
    config = tmp_path/'.local/share/xiaokai-lab-data-collection/workspaces/new'/suffix
    previous = tmp_path/'source/data-collection-app'/suffix
    docker = Docker(previous)
    assert recovery.start(config, docker) == 0
    assert len(docker.removed) == 5


def test_workspace_migration_from_another_users_home_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path/'me')
    suffix = 'umi_collection/vendor/galaxis/docker-compose.yml'
    config = tmp_path/'me/workspaces/new'/suffix
    docker = Docker(tmp_path/'other/source'/suffix)
    assert recovery.start(config, docker) == 1
    assert not docker.removed


def test_start_race_is_caught_by_id_reinspection(tmp_path):
    config = tmp_path/'docker-compose.yml'; docker = Docker(config)
    docker.on_reinspect = lambda item: item['State'].update(Status='running', Running=True)
    assert recovery.start(config, docker) == 1
    assert not docker.removed


def test_probe_permission_error_never_triggers_removal(tmp_path):
    config = tmp_path/'docker-compose.yml'; docker = Docker(config)
    docker.fail_diff = result(1, error='permission denied')
    assert recovery.start(config, docker) == 1
    assert not docker.removed


def test_failed_retry_is_not_repeated(tmp_path, capsys):
    config = tmp_path/'docker-compose.yml'; docker = Docker(config)
    docker.retry = result(9, error='RWLayer is still unexpectedly nil')
    assert recovery.start(config, docker) == 9
    assert sum(args[:2] == ['docker', 'compose'] and args[4] == 'up' for args in docker.calls) == 2
    assert 'still unexpectedly nil' in capsys.readouterr().err


def test_oneoff_workers_are_left_to_their_own_launcher(tmp_path):
    config = tmp_path/'docker-compose.yml'; docker = Docker(config)
    first = next(iter(docker.records))
    docker.records[first]['Config']['Labels']['com.docker.compose.oneoff'] = 'True'
    assert recovery.start(config, docker) == 0
    assert first not in docker.removed
