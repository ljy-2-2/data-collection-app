"""Bridge stdlib logging to ROS2 loggers.

Allows pure Python libraries (e.g. ``data_backend``) to use stdlib
``logging.getLogger(__name__)`` while having their output routed
through the ROS2 logging system (``/rosout``, ``rqt_console``).

Usage in a ROS node::

    from galaxis_core.ros_log_bridge import bridge_to_ros

    class MyNode(Node):
        def __init__(self):
            super().__init__("my_node")
            bridge_to_ros("data_backend", self.get_logger())
"""

import logging


class RosLogHandler(logging.Handler):
    """Forward stdlib log records to a ROS2 logger."""

    _LEVEL_MAP = {
        logging.DEBUG: "debug",
        logging.INFO: "info",
        logging.WARNING: "warn",
        logging.ERROR: "error",
        logging.CRITICAL: "fatal",
    }

    def __init__(self, ros_logger):
        super().__init__()
        self._ros = ros_logger

    def emit(self, record):
        method = self._LEVEL_MAP.get(record.levelno, "info")
        getattr(self._ros, method)(self.format(record))


def bridge_to_ros(package_name: str, ros_logger) -> None:
    """Route all stdlib logs from *package_name* through a ROS2 logger.

    Sets the stdlib logger level to DEBUG so ROS2 controls filtering.
    Safe to call multiple times — skips if handler already installed.
    """
    py_logger = logging.getLogger(package_name)
    if not any(isinstance(h, RosLogHandler) for h in py_logger.handlers):
        py_logger.addHandler(RosLogHandler(ros_logger))
        py_logger.setLevel(logging.DEBUG)
