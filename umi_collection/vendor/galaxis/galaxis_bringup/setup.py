from glob import glob
from setuptools import find_packages, setup

package_name = "galaxis_bringup"

setup(
    name=package_name,
    version="0.0.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", glob("launch/*.launch.py")),
        (
            "share/" + package_name + "/config",
            glob("config/*.json")
            + glob("config/galaxis_config.yaml")
            + glob("config/_runtime/single/galaxis_devices.yaml"),
        ),
    ],
    install_requires=[
        "setuptools",
    ],
    zip_safe=True,
    maintainer="Jumpei Arima",
    maintainer_email="jumpei_arima@mail.toyota.co.jp",
    description="ROS 2 package for galaxis bringup",
    license="Apache License 2.0",
    extras_require={
        "test": [
            "pytest",
        ],
    },
    entry_points={
        "console_scripts": [
            "camera_supervisor = galaxis_bringup.camera_supervisor:main",
            "camera_control_node = galaxis_bringup.camera_control_node:main",
            "center_camera_flip_node = galaxis_bringup.center_camera_flip_node:main",
            "encoder_node = galaxis_bringup.encoder_node:main",
            "g2_camera_bridge_node = galaxis_bringup.g2_camera_bridge_node:main",
            "gripper_double_click_node = galaxis_bringup.gripper_double_click_node:main",
            "portable_joy_command_node = galaxis_bringup.portable_joy_command_node:main",
        ],
    },
)
