"""Use one inherited host ADB endpoint for every collection subprocess."""
from __future__ import annotations

import os
import platform
import sys
from collections.abc import MutableMapping


def configure_adb_environment(env: MutableMapping[str, str] | None = None) -> MutableMapping[str, str]:
    """Leave explicit settings/PCs unchanged; isolate ARM host ADB from adbd.

    Orange Pi images ship a USB-gadget adbd that occupies the usual host ADB
    port 5037 and answers host requests with "device offline". An app-owned
    host server uses 15037 without stopping that system service. The local
    socket syntax deliberately omits a hostname: Ubuntu 22.04's ADB treats
    tcp:127.0.0.1:15037 as a remote server and refuses to start it automatically.
    No server is started here; the first hardware check does that through ADB.
    """
    target = os.environ if env is None else env
    if (sys.platform.startswith("linux")
            and platform.machine().lower() in {"aarch64", "arm64"}
            and not any(target.get(key) for key in (
                "ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_PORT", "ANDROID_ADB_SERVER_ADDRESS"))):
        target["ADB_SERVER_SOCKET"] = "tcp:15037"
    return target
