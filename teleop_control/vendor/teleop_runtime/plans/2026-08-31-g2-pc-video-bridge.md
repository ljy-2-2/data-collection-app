# G2 → PC latest-frame video bridge

Status: implemented; real G2 → PC → PICO display and simultaneous control verified (2026-09-01)
(2026-09-01)

## What

- Problem / goal: add the read-only PC half of the robot-view pipeline so the G2 head camera can later be displayed in the PICO headset without coupling video to the `63901` control channel.
- User-visible behavior: one launcher keeps the GDK camera reader inside `g2ns`, forwards validated `kHeadColor` JPEG frames to a host-side bridge, and exposes only the newest frame on TCP `63902`. A text observer verifies metadata, while a separate PC preview app displays the same JPEG stream on a local-only browser page.
- Success criteria: the binary stream preserves camera name, sequence, source/PC timestamps, dimensions, encoding, and a complete JPEG; fragmented TCP reads decode correctly; malformed or oversized frames fail closed; a one-slot mailbox overwrites old frames; a slow/disconnected client cannot make camera frames accumulate; GDK is always released; the preview forwards complete JPEGs and stops cleanly; all existing tests remain green; every output states that no physical robot command is sent.
- Out of scope: Unity/PICO changes, saving photos or video, restoring the removed `13579`/UDP receiver, audio, H.264/WebRTC, stereo rendering, changing TCP `63901`, IK, joint commands, or robot motion.

## How

- Approach: define a small robot-independent `VideoFrame` contract and versioned length-aware wire protocol under `teleop.vision`. The host `VideoBridge` accepts one Unix-domain camera producer and one TCP viewer, stores only the latest frame, and reconnects viewers without stopping capture. A thin G2 worker imports the already-validated `GDKObservationReader`, reads JPEG/RGB/8-bit `kHeadColor`, and sends it over the Unix socket. A supervisor starts the host bridge normally and the worker through the existing `g2-network run` boundary.
- Components / files touched: `src/teleop/vision/`, `apps/video_bridge.py`, `apps/g2_camera_worker.py`, `apps/observe_video_stream.py`, `apps/preview_video_stream.py`, `apps/run_g2_camera_worker.sh`, `apps/run_g2_video_bridge.sh`, focused tests, `README.md`, and this plan.
- Phases: RED protocol/mailbox tests; GREEN generic implementation; fake-socket end-to-end run; G2 worker lifecycle tests; full regression; optional user-run real-G2 read-only verification.
- Risks / unknowns: G2 source cadence and JPEG size vary; PICO decode latency is not measurable until the Unity client exists. Unix socket cleanup must reject non-socket paths, payload sizes are bounded, and network backpressure disconnects the viewer instead of building a queue.
- Safety boundary: camera reads only. The worker constructs `gdk.Camera` but no robot command sender, never calls a GDK write method, and always calls `gdk_release()`. No Unity file, route, ADB rule, existing control process, or physical joint state is changed.
- Verification plan: demonstrate focused tests failing before implementation and passing after it; run all `teleop-runtime` tests; compile Python and syntax-check shell; run an offline producer → Unix socket → host bridge → TCP consumer flow that proves only the newest frame survives; report real G2/PICO display as not yet verified.

## Result

- RED first failed because `teleop.vision` and the G2 camera worker did not exist. Review-driven RED tests then reproduced inaccurate overwrite counting, unsafe replacement of an active Unix socket, duplicate-source replay, and missing G2 resolution enforcement.
- GREEN now passes 154/154 runtime tests. Focused coverage validates fragmented TCP reads, complete metadata round-trip, oversized/malformed rejection, latest-only replacement, accurate delivery/drop accounting, active/non-socket path protection, GDK init/read/release lifecycle, duplicate timestamp suppression, `640x400` fail-closed behavior, complete-JPEG preview forwarding, MJPEG boundaries, the browser page, bounded preview, and clean stop requests.
- Python compilation and both shell syntax checks pass. A real local process run sent three JPEG-framed messages through Unix socket → `VideoBridge` → loopback TCP → `observe_video_stream.py`; the observer received 3/3 and the bridge reported `overwritten=0` because the consumer kept up.
- The new generic vision package contains no vendor import. Only `apps/g2_camera_worker.py` lazily imports `agibot_gdk`, and the reviewed files contain no joint-control, servo, IK, or robot-send call.
- A user-run physical-G2 session received 30 consecutive `kHeadColor` frames (`sequence=239..268`) at `640x400`, approximately 34 KB each, through TCP `63902`; after the first frame, reported PC-side bridge age was approximately `0.4..1.5 ms`, and every line reported `physical_robot_command_sent=False`. This verifies real G2 → PC transport, not end-to-end camera latency.
- The first GStreamer-window attempt was rejected during verification because this PC's JPEG decoder failed only when negotiated with its graphical video sink, even though the same JPEGs decoded successfully into a non-graphical sink. The delivered PC preview therefore uses a Python-standard-library, local-only HTTP page and browser MJPEG decoding; it requires no new package, saves nothing, and changes no Unity file.
- A local end-to-end process test sent 100 generated JPEG color-bar frames through the real versioned TCP reader into the preview service. Its HTML referenced `/stream.mjpg`, and the stream returned the multipart header plus a valid JPEG `FFD8` marker. The user then visually confirmed the physical G2 `kHeadColor` image, orientation, color, and browser behavior on the PC.
- Follow-up phase 2 upgraded the compatibility-named `VideoBridge` to `VideoHub`: viewers now connect concurrently with independent latest-frame sender threads. Automated tests verify two simultaneous viewers, disconnect isolation, and slow-viewer isolation.
- Follow-up phase 3 added the PICO `RobotVideoClient` and Unity view controls. Operator-visible acceptance confirmed the real G2 image in PICO at `640×400` and about `30 FPS`, draggable large view behavior, and black clearing after close. A later combined session ran this video path together with live direct-joint control; video remained independent from TCP `63901`, `XRFrame`, and joint safety.
- Not yet verified: PICO/Unity decoding and display, ADB `63902`, sustained FPS/end-to-end latency, and reconnect behavior on the headset. No Unity file was changed in this phase.
