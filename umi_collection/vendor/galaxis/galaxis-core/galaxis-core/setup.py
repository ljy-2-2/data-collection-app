from glob import glob
from setuptools import find_packages, setup

package_name = "galaxis_core"

setup(
    name=package_name,
    version="0.0.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ('share/' + package_name + '/launch', glob('launch/*.launch.py')),
        ('share/' + package_name + '/config', glob('config/*.yaml') + glob('config/*.yaml.sample')),
    ],
    install_requires=[
        "setuptools",
        "airoa_metadata @ git+https://github.com/airoa-org/airoa-metadata.git@development",
        "jsonschema",
        "requests",
        "mcap>=1.2.2,<2",
        "minio",
        ],
    zip_safe=True,
    maintainer="Takuya Okubo",
    maintainer_email="okubo.takuya@airoa.org",
    description="ROS 2 package for managing task recordings.",
    license="Apache License 2.0",
    extras_require={
        "test": [
            "pytest",
        ],
        "sentry": [
            "sentry-sdk",
        ],
    },
    entry_points={
        "console_scripts": [
            "record_manager = galaxis_core.record_manager:main",
            "hdf5_recorder = galaxis_core.hdf5_recorder:main",
            "task_receiver = galaxis_core.task_receiver:main",
            "metadata_handler = galaxis_core.metadata_handler:main",
            "task_sequence_manager = galaxis_core.task_sequence_manager:main",
            "task_command_dispatch_node = galaxis_core.task_command_dispatch_node:main",
            "storage_node = galaxis_core.storage_node:main",
            "recording_gate_node = galaxis_core.recording_gate_node:main",
            "robot_status_node = galaxis_core.robot_status_node:main",
            ],
    },
)
