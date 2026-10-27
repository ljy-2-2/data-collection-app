"""Real process shutdown, including the ignored SIGINT inherited from nohup."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def wait_until(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.04)
    raise AssertionError('Timed out waiting for test process')


def alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
    except (FileNotFoundError, ProcessLookupError):
        # /proc can disappear after open but before read when the child exits.
        return False


@pytest.mark.parametrize('shutdown_signal', [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize('stage', ['startup', 'ready'])
def test_signal_reaps_service_started_in_an_independent_session(tmp_path, shutdown_signal, stage):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    package = tmp_path / 'teleop_control/src/teleop_control'
    package.mkdir(parents=True)
    (package / '__init__.py').touch()
    (package / 'main.py').write_text('''
import http.server, os, signal
from pathlib import Path
signal.signal(signal.SIGINT, signal.default_int_handler)
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
    def do_POST(self):
        with Path('shutdown-requests').open('a') as f: f.write('shutdown\\n')
        self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
    def log_message(self, *_): pass
server = http.server.HTTPServer(('127.0.0.1', PORT), Handler)
Path('backend.pid').write_text(str(os.getpid()))
try: server.serve_forever()
except KeyboardInterrupt: pass
finally: server.server_close()
'''.replace('PORT', str(port)))
    harness = tmp_path / 'harness.py'
    harness.write_text(f'''
import signal, sys
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_IGN)
sys.path.insert(0, {str(ROOT / 'scripts')!r})
import run_app
run_app.ROOT = Path({str(tmp_path)!r})
run_app.read_env = lambda _: {{'LOCAL_CONTROL_PORT': '{port}', 'LOCAL_CONTROL_HOST': '127.0.0.1'}}
run_app.platform_urls = lambda _: {{}}
run_app.clear_previous_app_login = lambda _: None
sys.argv = ['run_app.py', '--backend-only', '--no-browser']
raise SystemExit(run_app.main())
''')
    log = tmp_path / 'supervisor.log'
    child_pid = None
    with log.open('w') as output:
        supervisor = subprocess.Popen([sys.executable, str(harness)], stdout=output,
                                      stderr=subprocess.STDOUT, start_new_session=True)
    try:
        wait_until(lambda: (tmp_path / 'backend.pid').exists())
        child_pid = int((tmp_path / 'backend.pid').read_text())
        assert os.getpgid(child_pid) == child_pid
        if stage == 'ready':
            wait_until(lambda: 'NATIVE_SERVICE_READY' in log.read_text())
        supervisor.send_signal(shutdown_signal)
        assert supervisor.wait(timeout=12) == 0, log.read_text()
        assert not alive(child_pid), log.read_text()
        with socket.socket() as sock:
            assert sock.connect_ex(('127.0.0.1', port)) != 0
        if stage == 'ready':
            assert (tmp_path / 'shutdown-requests').read_text().splitlines() == ['shutdown', 'shutdown']
    finally:
        for pid in (child_pid, supervisor.pid):
            if pid is not None and alive(pid):
                try: os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError: pass
        supervisor.wait(timeout=5)


def test_stalled_service_is_killed_and_reaped_as_a_group(tmp_path):
    sys.path.insert(0, str(ROOT / 'scripts'))
    import run_app
    # The leader and descendant both ignore graceful signals. Escalation must
    # reach the group, rather than terminate only the tracked parent process.
    script = tmp_path / 'stalled.py'
    script.write_text('''
import os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if len(sys.argv) == 1:
    child = subprocess.Popen([sys.executable, __file__, 'child'])
    Path(__file__ + '.child').write_text(str(child.pid))
else:
    Path(__file__ + '.ready').touch()
while True: time.sleep(1)
''')
    process = subprocess.Popen([sys.executable, str(script)], start_new_session=True)
    child_pid = None
    try:
        wait_until(lambda: Path(str(script) + '.ready').exists())
        child_pid = int(Path(str(script) + '.child').read_text())
        run_app.stop_process_group(process, timeout=.1)
        assert process.returncode == -signal.SIGKILL
        wait_until(lambda: not alive(child_pid), timeout=2)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
