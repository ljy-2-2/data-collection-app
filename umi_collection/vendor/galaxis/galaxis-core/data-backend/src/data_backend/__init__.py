"""data_backend — S3 storage utilities for recording upload, GC, and state tracking.

Note: ``data_backend`` is a working name.  This package will grow to include
recording logic (rosbag management, metadata) in the future.

Submodules are imported explicitly to avoid pulling in heavy dependencies
(e.g. the S3 client) at package import time::

    from data_backend.gc import GCConfig, run_gc_cycle_with_result
    from data_backend.upload import upload_recording
    from data_backend.upload_state import UploadStateDB
"""
