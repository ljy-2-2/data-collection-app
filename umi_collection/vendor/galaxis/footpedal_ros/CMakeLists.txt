cmake_minimum_required(VERSION 3.8)
project(footpedal_ros)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# find dependencies
find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(std_msgs REQUIRED)

find_package(PkgConfig REQUIRED)
pkg_check_modules(HIDAPI REQUIRED hidapi-libusb)

include_directories(${HIDAPI_INCLUDE_DIRS} include)

add_executable(footpedal_node src/footpedal_node.cpp)
target_link_libraries(footpedal_node ${HIDAPI_LIBRARIES})
ament_target_dependencies(footpedal_node rclcpp sensor_msgs std_msgs)


install(TARGETS
  footpedal_node
  DESTINATION lib/${PROJECT_NAME}
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
