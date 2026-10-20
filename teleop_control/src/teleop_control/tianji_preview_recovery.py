"""Reconnect passive previews after an audited camera gap; never repair an episode."""
import re
import time


def preview_only(manager, session):
    return (not (session and session.attached)
            and not manager.collection.armed and not manager.collection.active())


def recover_preview_gap(manager, client, session, attempt):
    if (attempt >= 3 or not preview_only(manager, session)
            or manager.camera_stop.is_set()
            or time.monotonic()-manager.last_access >= 8):
        return False
    manager.camera_phase = 'recovering'
    # Give cleanup and enumeration time to finish. Stop/close interrupts this
    # wait; a camera reconnect never changes VR anchors or robot commands.
    if manager.camera_stop.wait(max(2.1, manager.camera_cooldown+.1) * (attempt+1)):
        return False
    if (not preview_only(manager, session)
            or time.monotonic()-manager.last_access >= 8):
        return False
    status = client.status()
    if (status.get('idle') is not True or not status.get('fault')
            or status.get('camera_hotplug_recovery') is not True
            or status.get('recoverable_camera_fault') is not True):
        return False
    # All cameras may be unplugged. Keep discovery alive without burning the
    # retry budget or attempting capture until at least one has re-enumerated.
    while True:
        cameras = client.list_cameras()
        inventory = {c['id'].replace('-', '_'):c['id'] for c in cameras
                     if c.get('available') and isinstance(c.get('id'), str)
                     and re.fullmatch(r'[A-Za-z0-9_-]+', c['id'])}
        with manager.lock:
            manager.camera_inventory = inventory
            for camera in set(manager.frames)-set(inventory):
                del manager.frames[camera]
        if inventory:
            break
        manager.camera_phase = 'connecting'
        if (manager.camera_stop.wait(1.) or not preview_only(manager, session)
                or time.monotonic()-manager.last_access >= 8):
            return False
    client.recover_camera()
    status = client.status()
    if status.get('fault') or status.get('idle') is not True:
        return False
    manager._diagnostic_event('preview_gap_recovered', attempt=attempt+1)
    return True


def inventory_changed(manager, session, metadata):
    if not preview_only(manager, session) or not isinstance(metadata.get('cameras'), list):
        return False
    connected = {camera.get('id') for camera in metadata['cameras'] if camera.get('available')}
    if connected == set(manager.camera_inventory.values()):
        return False
    manager.camera_phase = 'reconnecting'
    manager._diagnostic_event('preview_inventory_changed', connected=sorted(connected))
    return True
