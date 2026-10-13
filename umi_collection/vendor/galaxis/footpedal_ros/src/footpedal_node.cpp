#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/joy.hpp>
#include <hidapi.h>
#include <cstdio>
#include <cstring>
#include <chrono>
#include <thread>
#include <mutex>
#include <atomic>
#include <std_msgs/msg/bool.hpp>

using namespace std::chrono_literals;

class FootPedalNode : public rclcpp::Node
{
public:
    FootPedalNode() : Node("footpedal_node"), dev_(nullptr), pedal_thread_(nullptr), running_(true)
    {
        init_device();
        latest_joy_msg_.buttons.resize(3);
        latest_joy_msg_.buttons[0] = 0;
        latest_joy_msg_.buttons[1] = 0;
        latest_joy_msg_.buttons[2] = 0;

        foot_pedal_pub_ = this->create_publisher<sensor_msgs::msg::Joy>("footpedal_states", 10);

        pedal_thread_ = std::make_unique<std::thread>(&FootPedalNode::callback_pedal_state, this);

        timer_ = this->create_wall_timer(100ms, std::bind(&FootPedalNode::publish_footpedal_state, this));

    }

    ~FootPedalNode()
    {
        running_.store(false);
        if (pedal_thread_ && pedal_thread_->joinable()) {
            pedal_thread_->join();
        }
        if (dev_) {
            hid_close(dev_);
            dev_ = nullptr;
        }
        hid_exit();
    }

private:
    hid_device *dev_;
    rclcpp::Publisher<sensor_msgs::msg::Joy>::SharedPtr foot_pedal_pub_;
    rclcpp::TimerBase::SharedPtr timer_;
    std::unique_ptr<std::thread> pedal_thread_;
    std::atomic<bool> running_;
    std::mutex pedal_mutex_;
    sensor_msgs::msg::Joy latest_joy_msg_;

    void init_device()
    {
        unsigned short vid_pid_pair[2] = {0x3553, 0xb001};
        dev_ = hid_open(vid_pid_pair[0], vid_pid_pair[1], nullptr);
        if (dev_ != nullptr)
        {
            hid_set_nonblocking(dev_, 1);
            RCLCPP_INFO(this->get_logger(), "Device connected with VID:PID %04hx:%04hx", vid_pid_pair[0], vid_pid_pair[1]);
            return;
        }
        RCLCPP_ERROR(this->get_logger(), "Cannot find footswitch device. Check connection and permissions.");
        throw std::runtime_error("Failed to initialize pedal device");
    }

    void callback_pedal_state()
    {
        while (rclcpp::ok() && running_.load()) {
            if (dev_ == nullptr) {
                RCLCPP_WARN(this->get_logger(), "Device handle lost; stopping pedal thread");
                break;
            }
            unsigned char query[8] = {0x01, 0x82, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00};
            unsigned char response[8];

            int r = hid_write(dev_, query, sizeof(query));
            if (r < 0) {
                RCLCPP_ERROR(this->get_logger(), "Error writing to device: %ls", hid_error(dev_));
                break;
            }
            r = hid_read(dev_, response, sizeof(response));
            if (r == 0) {
                std::this_thread::sleep_for(5ms);
                continue;
            }
            if (r < 0) {
                RCLCPP_ERROR(this->get_logger(), "Error reading from device");
                break;
            }
            std::lock_guard<std::mutex> lock(pedal_mutex_);
            if (response[3] == 0x04 || response[4] == 0x04 || response[5] == 0x04) {
                latest_joy_msg_.buttons[0] = 1;
            } else {
                latest_joy_msg_.buttons[0] = 0;
            }
            if (response[3] == 0x05 || response[4] == 0x05 || response[5] == 0x05) {
                latest_joy_msg_.buttons[1] = 1;
            } else {
                latest_joy_msg_.buttons[1] = 0;
            }
            if (response[3] == 0x06 || response[4] == 0x06 || response[5] == 0x06) {
                latest_joy_msg_.buttons[2] = 1;
            } else {
                latest_joy_msg_.buttons[2] = 0;
            }
            std::this_thread::sleep_for(10ms);
        }
    }

    void publish_footpedal_state()
    {
        sensor_msgs::msg::Joy joy_msg;
        joy_msg.header.stamp = this->get_clock()->now();
        joy_msg.header.frame_id = "foot_pedal";
        joy_msg.buttons.resize(3);
        std::lock_guard<std::mutex> lock(pedal_mutex_);
        joy_msg.buttons = latest_joy_msg_.buttons;
        foot_pedal_pub_->publish(joy_msg);
    }
};

int main(int argc, char *argv[])
{
    rclcpp::init(argc, argv);
    auto node = std::make_shared<FootPedalNode>();

    try {
        rclcpp::spin(node);
    } catch (const std::exception &e) {
        RCLCPP_ERROR(node->get_logger(), "Error during execution: %s", e.what());
    }

    RCLCPP_INFO(node->get_logger(), "Shutting down footpedal node...");
    rclcpp::shutdown();
    return 0;
}
