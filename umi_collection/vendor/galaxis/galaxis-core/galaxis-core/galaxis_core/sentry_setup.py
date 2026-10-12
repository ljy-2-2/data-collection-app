import os


def init_sentry():
    dsn = os.environ.get("SENTRY_DSN", "")
    if not dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("ENV", "development"),
        release=os.environ.get("GIT_HASH", "unknown"),
    )
