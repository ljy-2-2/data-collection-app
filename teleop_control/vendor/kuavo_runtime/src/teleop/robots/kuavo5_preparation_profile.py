"""Reviewed robot-local preparation dependencies; import performs no I/O.

Update only through the formal release workflow after checking the target robot.
Motion profiles and gripper endpoints come from that release core.json.
"""

CONTROLLER_ROOT = '/opt/galaxis/kuavo-controller/releases/20261019T095454Z-23b228673ec5'
CONTROLLER_RUNTIME_SHA256 = '23b228673ec532a5e893f19a3f4036705eef86cd2d2575f204c85d55579e1f8e'
CONTROLLER_CONFIG_SHA256 = 'c6d7ff805fbfe038fdb0b47efd60b39dd431ea9cb2cf6cafccc9699cecebcd21'
CONTROLLER_LAUNCHER_SHA256 = '8b6f28ed5d707a4694b50185bc9a00c6a4b79f7fbc66ff2297c929c49eb34323'
VENDOR_PINS = {'/home/lab/kuavo-ros-control/src/kuavo-ros-control-lejulib/hardware_node/include/hardware_node/hardware_node.h': '77e5c73a8757cd1937acaf0cd4ad52303aa3247e34afb9b0169ad3b3e8eec878', '/home/lab/kuavo-ros-control/src/kuavo-ros-control-lejulib/hardware_node/src/hardware_node.cc': 'aa8c1af21062f44dbc1327767997e250afc882471058eb96ae5e5fff371d216f'}
STATE_DIRECTORY = '/var/lib/galaxis-kuavo-preparation'
