"""Run with system Python for libsecret GI, not the bundled backend Python.

Passwords are passed through pipes, never argv, environment variables or files.
API reference: https://gnome.pages.gitlab.gnome.org/libsecret/libsecret-python-examples.html
"""
import json
import sys


def main():
    try:
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret

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
                return 1
        elif operation == "lookup":
            password = Secret.password_lookup_sync(schema, attrs, None) or ""
            print(json.dumps({"password": password}))
            return 0
        elif operation == "clear":
            Secret.password_clear_sync(schema, attrs, None)
        else:
            return 1
        print("{}")
        return 0
    except Exception:
        # Do not print exception details: they may include credential material.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
