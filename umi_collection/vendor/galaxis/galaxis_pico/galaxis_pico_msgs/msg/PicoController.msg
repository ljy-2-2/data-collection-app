# Header.stamp = ROS clock at receive on the PC. Identical to the matching
# /pico/hmd/state and the other controller's message from the same source frame.
std_msgs/Header header

# PICO application timestamp written while assembling the Tracking payload.
# The current Unity sender computes Galaxis timeStampNs from DateTime.UtcNow as
# Unix epoch nanoseconds; it is not a hardware pose or monotonic timestamp.
uint64 device_time_ns

# PC monotonic clock (nanoseconds) at receive (time.monotonic_ns()). See
# PicoHmd.msg for the post-processing alignment formula.
uint64 pc_monotonic_ns

# Sequence number assigned by the bridge, increasing once per received
# Galaxis tracking sample.
uint32 seq

# PICO device identifier reported by the Galaxis connection handshake. The
# bridge uses `galaxis-pico4-ultra` until the headset identifies itself.
string pico_id

# Controller pose expressed in the PICO tracking-space origin
# (the Galaxis tracking origin on the device side; published as TF frame
# `pico_origin` on the ROS side, name configurable via the bridge's
# `parent_frame` parameter).
# Coordinate convention: ROS (X forward, Y left, Z up, right-handed). The
# bridge converts the PICO SDK native right-handed pose (X right, Y up,
# Z backward) at the boundary. This matches the parent of /tf frames
# `pico_left_controller` /
# `pico_right_controller`, so a consumer can use either this message or the
# TF tree without re-mapping coordinates.
# Note: this is the raw PICO controller pose, not the offset-corrected
# `*_hand_root` frame computed downstream by the bridge for IK consumers.
geometry_msgs/Vector3 position
geometry_msgs/Quaternion rotation

# Tracking flags inferred from the Galaxis controller pose sample.
bool tracked
bool valid

# Button / trigger / grip / joystick inputs are published separately as
# sensor_msgs/Joy on /pico/controller/{left,right}/joy (see pico_bridge_node).
# Battery state is published separately as sensor_msgs/BatteryState on
# /pico/controller/{left,right}/battery.
