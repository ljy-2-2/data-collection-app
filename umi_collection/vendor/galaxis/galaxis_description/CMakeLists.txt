cmake_minimum_required(VERSION 3.8)
project(galaxis_description)

find_package(ament_cmake REQUIRED)

install(
  FILES urdf/galaxis_hand.urdf.xacro
  DESTINATION share/${PROJECT_NAME}/urdf
)

ament_package()
