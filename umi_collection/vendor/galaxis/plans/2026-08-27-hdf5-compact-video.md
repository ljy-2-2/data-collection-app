# HDF5 compact video storage

Status: implemented (2026-08-27)

## What

- Problem / goal: Current HDF5 v2 keeps JPEG `CompressedImage` CDR bytes and is almost as large as MCAP. Make new `--format hdf5` recordings materially smaller using the measured best-quality option: H.264 CRF 14, 640×480, 30 FPS.
- User-visible behavior: A completed HDF5 Episode contains `<episode>.h5`, `center_camera.mp4`, `right_camera.mp4`, `manifest.json`, and `meta.json`; rosbag output is unchanged. Stop/next-round, monitor count, Ctrl+C recovery, and operator ownership retain current behavior.
- Success criteria: Non-image topic/type/QoS/timestamp/order/CDR bytes validate exactly against staging MCAP; every `CompressedImage` maps once to a camera frame and original timestamp; MP4 probe confirms H.264, source resolution, 30 FPS, and exact frame count; manifest is committed last; staging is deleted only after payload and metadata validation; focused tests and one real sample pass; CRF 14 real-sample size is reported.
- Out of scope: Rewriting existing completed Episodes, bit-exact preservation of JPEG image CDR in compact mode, H.265, changing acquisition resolution/FPS, and direct LeRobot ingestion.

## How

- Approach: Keep MCAP as crash-safe staging. Stream only `sensor_msgs/msg/CompressedImage` payloads through FFmpeg/libx264 while writing a v3 HDF5 global timeline and exact non-image CDR. Write `.partial` artifacts, validate HDF5/video/source mappings, publish files, then atomically publish a SHA-256 manifest. Revalidation makes interrupted publication recoverable.
- Components / files touched: `hdf5_recorder.py`, `record_manager.py`, package/container dependencies, HDF5 and lifecycle tests, architecture documentation.
- Phases: implement schema/encoder/validator; wire lifecycle/recovery; verify tests and real sample; remove benchmark-only artifacts.
- Risks / unknowns: H.264 changes image pixels; FFmpeg child termination must not delay container shutdown; camera frame timing must remain in HDF5 even though MP4 is constant-FPS; current uploader only uploads top-level files, so videos remain top-level.
- Safety boundary: Existing `/home/ljy/galaxis_data` Episodes are read-only and never migrated or deleted. Staging is authoritative until the full compact bundle and `meta.json` pass validation. Only the explicitly created benchmark directory and scratch script are removed.
- Verification plan: 15 focused tests passed. A read-only 255,762,140-byte real MCAP converted to 842,259-byte HDF5 plus 28,329,470 bytes of video; 30,326 messages, exact non-image SHA-256, 633/623 video frames, 640×480, 30 FPS, first conversion and recovery revalidation all passed. Ruff and diff checks passed; benchmark and temporary validation artifacts were removed.
