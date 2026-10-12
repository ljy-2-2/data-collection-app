# Header.stamp = ROS clock at receive on the PC. Identical to the matching
# /pico/controller/left/state and /pico/controller/right/state messages from
# the same source frame, so cross-topic joining is trivial.
std_msgs/Header header

# PICO application timestamp written while assembling the Tracking payload.
# The current Unity sender computes Galaxis timeStampNs from DateTime.UtcNow as
# Unix epoch nanoseconds; it is not a hardware pose or monotonic timestamp.
uint64 device_time_ns

# PC monotonic clock (nanoseconds) at receive (time.monotonic_ns()).
# Combined with device_time_ns over a stable-clock sliding window in
# post-processing:
#   offset_ns = median(pc_monotonic_ns - device_time_ns)
#   aligned_pc_mono_ns = device_time_ns + offset_ns
uint64 pc_monotonic_ns

# Sequence number assigned by the bridge, increasing once per received
# Galaxis tracking sample.
uint32 seq

# PICO device identifier reported by the Galaxis connection handshake. The
# bridge uses `galaxis-pico4-ultra` until the headset identifies itself.
string pico_id

# HMD pose expressed in the PICO tracking-space origin
# (the Galaxis tracking origin on the device side; published as TF frame
# `pico_origin` on the ROS side, name configurable via the bridge's
# `parent_frame` parameter).
# Coordinate convention: ROS (X forward, Y left, Z up, right-handed). The
# bridge converts the PICO SDK native right-handed pose (X right, Y up,
# Z backward) at the boundary. This matches the parent of /tf frame
# `pico_hmd`, so a consumer
# can use either this message or the TF tree without re-mapping coordinates.
geometry_msgs/Vector3 position
geometry_msgs/Quaternion rotation
