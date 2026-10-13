"""Pytest configuration for data_backend tests."""


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: marks tests requiring a live S3 instance"
    )
