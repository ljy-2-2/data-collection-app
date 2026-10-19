"""Exercise real TCP/IPC while a ROS-like publisher is deliberately blocked."""
import json
import socket
import threading
import time

from galaxis_pico_bridge.transport.galaxis_tcp import IsolatedGalaxisTransport


def packet(seq):
    tracking = {'timeStampNs': seq * 10_000_000,
                'Controller': {'left': {'pose': '0,0,0,0,0,0,1',
                                        'primaryButton': seq == 10}}}
    payload = json.dumps({'functionName': 'Tracking', 'value': json.dumps(tracking)}).encode()
    return bytes([0x3f, 0x6d]) + len(payload).to_bytes(4, 'little') + payload + bytes(8) + bytes([0xa5])


def wait_for(check, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(.01)
    raise AssertionError('Timed out')


def start(callback, queue_size=256):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    transport = IsolatedGalaxisTransport(host='127.0.0.1', port=port,
                                         on_frame=callback, queue_size=queue_size)
    transport.start_listener()
    wait_for(lambda: transport.metrics()['listening'])
    return transport, socket.create_connection(('127.0.0.1', port))


def test_receive_clock_and_button_edges_survive_blocked_publication():
    frames = []
    entered, release = threading.Event(), threading.Event()
    def publish(frame):
        if not frames:
            entered.set()
            release.wait(5)
        frames.append(frame)
    transport, client = start(publish)
    try:
        client.sendall(packet(1))
        assert entered.wait(3)
        for seq in range(2, 21):
            client.sendall(packet(seq))
            time.sleep(.01)
        # Confirm receipt is still live although no publication has returned.
        wait_for(lambda: transport.metrics()['fps'] > 50)
        time.sleep(.15)
        released_at = time.time_ns()
        release.set()
        wait_for(lambda: len(frames) == 20)
        assert [f.seq for f in frames] == list(range(1, 21))
        assert [f.device_time_ns for f in frames] == [n * 10_000_000 for n in range(1, 21)]
        assert frames[9].raw['buttonXPressed'] is True
        assert frames[10].raw['buttonXPressed'] is False
        assert all(f.pc_received_wall_ns < released_at - 50_000_000 for f in frames)
        assert frames[-1].pc_monotonic_ns - frames[1].pc_monotonic_ns > 100_000_000
        offsets = [f.pc_received_wall_ns - f.pc_monotonic_ns for f in frames]
        assert max(offsets) - min(offsets) < 20_000_000
    finally:
        release.set()
        client.close()
        transport.stop()
    assert not transport.metrics()['connected']


def test_queue_overflow_is_a_persistent_fault_not_silent_loss():
    release = threading.Event()
    transport, client = start(lambda frame: release.wait(5), queue_size=2)
    try:
        client.sendall(b''.join(packet(n) for n in range(1, 30)))
        wait_for(lambda: 'overflow' in transport.metrics()['listen_error'])
        assert not transport.metrics()['connected']
        time.sleep(.1)
        assert 'overflow' in transport.metrics()['listen_error']
    finally:
        release.set()
        client.close()
        transport.stop()


def test_receiver_releases_port_after_parent_crash():
    import os
    import subprocess
    import sys
    from pathlib import Path
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    code = '''
import time
from galaxis_pico_bridge.transport.galaxis_tcp import IsolatedGalaxisTransport
transport = IsolatedGalaxisTransport(host='127.0.0.1', port=PORT, on_frame=lambda _: None)
transport.start_listener()
while not transport.metrics()['listening']: time.sleep(.01)
print('READY', flush=True)
time.sleep(30)
'''.replace('PORT', str(port))
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    parent = subprocess.Popen([sys.executable, '-u', '-c', code], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=env)
    try:
        # Prove the port was bound before simulating an ungraceful ROS exit.
        import select
        assert select.select([parent.stdout], [], [], 8)[0]
        assert parent.stdout.readline().strip() == 'READY'
        parent.kill()
        parent.wait(timeout=3)
        def released():
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1):
                    return False
            except ConnectionRefusedError:
                return True
        wait_for(released, timeout=5)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)
        parent.stdout.close()
        parent.stderr.close()
