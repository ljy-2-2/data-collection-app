#ifndef GALAXIS_TEST_ARDUINO_H
#define GALAXIS_TEST_ARDUINO_H

#include <cstddef>
#include <cstdint>
#include <iomanip>
#include <sstream>
#include <string>

using String = std::string;
constexpr double PI = 3.141592653589793238462643383279502884;
constexpr int OUTPUT = 1;
constexpr int HIGH = 1;
constexpr int LOW = 0;
constexpr int HEX = 16;

extern uint64_t fakeMicroseconds;
inline uint32_t millis() { return static_cast<uint32_t>(fakeMicroseconds / 1000U); }
inline void delay(uint32_t ms) { fakeMicroseconds += static_cast<uint64_t>(ms) * 1000U; }
inline void delayMicroseconds(uint32_t us) { fakeMicroseconds += us; }
inline void pinMode(int, int) {}
inline void digitalWrite(int, int) {}

class FakeSerial
{
public:
    uint32_t baud = 0;
    bool connected = true;
    std::ostringstream output;

    void begin(uint32_t value) { baud = value; }
    explicit operator bool() const { return connected; }
    template <typename T> void print(const T &value) { output << value; }
    void print(uint8_t value, int base)
    {
        if (base == HEX) output << std::hex << std::uppercase;
        output << static_cast<unsigned>(value) << std::dec;
    }
    template <typename T> void println(const T &value) { print(value); output << '\n'; }
    void println(double value, int digits)
    {
        output << std::fixed << std::setprecision(digits) << value << '\n';
    }
    void clear() { output.str(""); output.clear(); }
    std::string str() const { return output.str(); }
};
extern FakeSerial Serial;

#endif
