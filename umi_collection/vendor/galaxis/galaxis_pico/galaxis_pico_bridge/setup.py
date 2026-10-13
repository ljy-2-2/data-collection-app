from setuptools import find_packages, setup

package_name = "galaxis_pico_bridge"

setup(
    name=package_name,
    version="0.0.1",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=[
        "setuptools",
    ],
    zip_safe=True,
    maintainer="Jumpei Arima, Takuya Okubo",
    maintainer_email="jumpei_arima@mail.toyota.co.jp, okubo.takuya@airoa.org",
    description="ROS 2 bridge for Galaxis PICO 4 Ultra HMD and controllers.",
    license="Apache License 2.0",
    extras_require={
        "test": [
            "pytest",
        ],
    },
    entry_points={
        "console_scripts": [
            "pico_bridge_node = galaxis_pico_bridge.pico_bridge_node:main",
        ],
    },
)
