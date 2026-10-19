"""Run with system Python for libsecret GI, not the bundled backend Python.

Passwords are passed through pipes, never argv, environment variables or files.
API reference: https://gnome.pages.gitlab.gnome.org/libsecret/libsecret-python-examples.html
"""
import json
import os
from pathlib import Path
import stat
import sys


def desktop_bus_address():
    """Recover the same user's systemd desktop bus when a launcher omitted it."""
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return True
    # Do not start a private dbus session: its transient keyring would disappear
    # on exit and the saved credentials would be missing on the next launch.
    bus = Path("/run/user") / str(os.getuid()) / "bus"
    try:
        status = bus.stat()
        if status.st_uid == os.getuid() and stat.S_ISSOCK(status.st_mode):
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + str(bus)
            return True
    except OSError:
        pass
    return False


def fail(code):
    # Only fixed machine-readable codes cross the process boundary. GLib error
    # messages can contain attributes or other credential-related information.
    print(json.dumps({"error": code}))
    return 1


def provider_available(Gio, GLib):
    """Differentiate a missing provider from a locked/inactive installed one."""
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    def query(method, params):
        return bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", method, params, None,
            Gio.DBusCallFlags.NONE, 3000, None,
        ).unpack()[0]
    name = "org.freedesktop.secrets"
    if query("NameHasOwner", GLib.Variant("(s)", (name,))):
        return True
    # XFCE does not run GNOME's desktop autostart entry, but D-Bus can still
    # activate an installed keyring. Absence from both lists means installing
    # the provider is necessary; logging out cannot repair that condition.
    return name in query("ListActivatableNames", None)


def main():
    try:
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Gio, GLib, Secret
    except (ImportError, ValueError):
        return fail("missing_dependency")

    if not desktop_bus_address():
        return fail("no_session_bus")
    try:
        if not provider_available(Gio, GLib):
            return fail("missing_provider")
        # D-Bus activates an installed Secret Service provider automatically.
        Secret.Service.get_sync(Secret.ServiceFlags.OPEN_SESSION, None)
    except Exception:
        return fail("service_unavailable")
    try:
        operation, scope, key = sys.argv[1:]
        schema = Secret.Schema.new(
            "com.xiaokailab.Collection.Login", Secret.SchemaFlags.NONE,
            {"scope": Secret.SchemaAttributeType.STRING,
             "credential_id": Secret.SchemaAttributeType.STRING},
        )
        attrs = {"scope": scope, "credential_id": key}
        if operation == "store":
            password = json.load(sys.stdin)["password"]
            if not Secret.password_store_sync(
                schema, attrs, Secret.COLLECTION_DEFAULT,
                "小凯实验室数采平台 · 登录密码", password, None,
            ):
                return fail("cancelled")
            # Report success only after a fresh read observes the stored value.
            # This catches provider failures without exposing the password.
            if Secret.password_lookup_sync(schema, attrs, None) != password:
                Secret.password_clear_sync(schema, attrs, None)
                return fail("verification_failed")
        elif operation == "lookup":
            password = Secret.password_lookup_sync(schema, attrs, None) or ""
            print(json.dumps({"password": password}))
            return 0
        elif operation == "clear":
            Secret.password_clear_sync(schema, attrs, None)
        else:
            return fail("invalid_operation")
        print("{}")
        return 0
    except Exception as exc:
        # Do not print exception details: they may include credential material.
        if hasattr(exc, "matches"):
            if exc.matches(Secret.error_get_quark(), Secret.Error.IS_LOCKED):
                return fail("locked")
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                return fail("cancelled")
        return fail("unavailable")


if __name__ == "__main__":
    raise SystemExit(main())
