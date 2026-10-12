// Host regression of the real driver and sketch; stubs stay outside the sketch.
// g++ -std=c++11 -Wall -Wextra -Werror -pedantic -Ifirmware/tests/stubs firmware/tests/test_mt6835_firmware.cpp firmware/ESP32C6_MT7V0/MT6835.cpp -o /tmp/test_mt6835_firmware
// /tmp/test_mt6835_firmware
#include <Arduino.h>
#include <SPI.h>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <vector>

uint64_t fakeMicroseconds = 0;
FakeSerial Serial;
SPIClass SPI;
#include "../ESP32C6_MT7V0/ESP32C6_MT7V0.ino"

namespace
{
constexpr uint32_t counts = 1U << 21U;
constexpr double turn = 2.0 * PI;
int checks = 0;

void expect(bool condition, const char *message)
{
    ++checks;
    if (!condition)
    {
        std::cerr << "FAIL: " << message << '\n';
        std::exit(EXIT_FAILURE);
    }
}

void near(double actual, double expected, const char *message, double tolerance = 5.1e-9)
{
    expect(std::isfinite(actual) && std::fabs(actual - expected) <= tolerance, message);
}

// Independent polynomial division, rather than calling the driver's CRC when
// constructing replies. CRC-8/SMBUS check vector "123456789" is 0xF4.
uint8_t referenceCrc(const uint8_t *data, size_t size)
{
    unsigned remainder = 0;
    for (size_t byte = 0; byte < size; ++byte)
    {
        remainder ^= static_cast<unsigned>(data[byte]) << 8U;
        for (unsigned bit = 0; bit < 8; ++bit)
        {
            if (remainder & (1U << (15U - bit)))
                remainder ^= 0x107U << (7U - bit);
        }
        remainder <<= 8U;
    }
    return static_cast<uint8_t>(remainder >> 8U);
}

SPIClass::Frame frame(uint32_t raw, uint8_t flags = 0)
{
    SPIClass::Frame reply = {{0, 0, static_cast<uint8_t>(raw >> 13U),
        static_cast<uint8_t>(raw >> 5U), static_cast<uint8_t>((raw << 3U) | flags), 0}};
    reply[5] = referenceCrc(reply.data() + 2, 3);
    return reply;
}

void testDriver()
{
    const uint8_t check[] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};
    expect(referenceCrc(check, sizeof(check)) == 0xF4, "reference CRC known vector");
    expect(MT6835::crc8(check, sizeof(check)) == 0xF4, "driver CRC known vector");
    const uint8_t high[] = {0xFF, 0xFF, 0xFF};
    expect(MT6835::crc8(high, sizeof(high)) == 0x0F, "all-high angle payload CRC");

    SPIClass bus;
    MT6835 sensor(bus, 20);
    sensor.begin(21, 23, 22);
    expect(bus.pins == std::array<int, 4>{{21, 23, 22, 20}}, "SPI pins");
    MT6835AngleSample sample = {12345, 0xAA};
    bus.responses.push_back(frame(0));
    expect(sensor.readAngle(sample) == MT6835ReadStatus::Ok, "all-zero legal angle accepted");
    expect(sample.rawAngle == 0 && sample.statusFlags == 0, "zero decoded exactly");
    expect(sensor.getDiagnostic() == "OK", "zero diagnostic");

    bus.responses.push_back(frame(counts - 1U));
    expect(sensor.readAngle(sample) == MT6835ReadStatus::Ok, "maximum angle accepted");
    expect(sample.rawAngle == counts - 1U, "21-bit maximum decoded exactly");

    sample = {54321, 0xAA};
    auto corrupt = frame(77777);
    corrupt[5] ^= 1;
    bus.responses.push_back(corrupt);
    expect(sensor.readAngle(sample) == MT6835ReadStatus::CrcError, "bad CRC rejected");
    expect(sample.rawAngle == 54321 && sample.statusFlags == 0xAA, "CRC preserves caller sample");
    expect(sensor.getDiagnostic() == "CRC_ERROR", "CRC diagnosis");

    bus.responses.push_back({{0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF}});
    expect(sensor.readAngle(sample) == MT6835ReadStatus::BusError, "all-FF reply rejected");
    expect(sample.rawAngle == 54321 && sample.statusFlags == 0xAA, "bus error preserves caller sample");
    expect(sensor.getDiagnostic() == "BUS_ERROR", "bus diagnosis");

    for (uint8_t flags = 1; flags <= 7; ++flags)
    {
        const uint32_t raw = 456789U + flags;
        bus.responses.push_back(frame(raw, flags));
        expect(sensor.readAngle(sample) == MT6835ReadStatus::SensorFault, "every nonzero status combination rejects measurement");
        expect(sample.rawAngle == raw && sample.statusFlags == flags, "fault retains raw diagnostic evidence");
        const std::string message = sensor.getDiagnostic();
        expect((message.find("OVERSPEED") != std::string::npos) == bool(flags & 1U), "overspeed diagnosis bit");
        expect((message.find("WEAK_MAGNETIC_FIELD") != std::string::npos) == bool(flags & 2U), "weak field diagnosis bit");
        expect((message.find("UNDERVOLTAGE") != std::string::npos) == bool(flags & 4U), "undervoltage diagnosis bit");
        double radians = 123.456;
        uint8_t statusFlags = 0xAA;
        bus.responses.push_back(frame(raw, flags));
        expect(sensor.readAngleRadians(radians, statusFlags) == MT6835ReadStatus::SensorFault, "radians API rejects sensor faults");
        near(radians, 123.456, "sensor fault preserves radians");
        expect(statusFlags == flags, "radians API supplies fault flags");
    }

    double radians = 123.456;
    uint8_t flags = 0xAA;
    bus.responses.push_back(corrupt);
    expect(sensor.readAngleRadians(radians, flags) == MT6835ReadStatus::CrcError, "radians API CRC error");
    near(radians, 123.456, "CRC preserves radians");
    expect(flags == 0xAA, "CRC preserves caller status flags");
    bus.responses.push_back(frame(0));
    expect(sensor.readAngleRadians(radians, flags) == MT6835ReadStatus::Ok, "radians API accepts zero");
    near(radians, 0.0, "zero radians");
    expect(flags == 0, "successful read clears fault status");
    expect(bus.responses.empty() && !bus.transaction, "all driver responses consumed and transaction closed");
}

uint32_t rawFromRadians(double radians)
{
    return static_cast<uint32_t>(std::llround(radians * counts / turn));
}

std::vector<double> values(const std::string &output)
{
    std::vector<double> result;
    std::istringstream lines(output);
    std::string line;
    while (std::getline(lines, line))
    {
        if (line.compare(0, 5, "R003,") == 0) result.push_back(std::stod(line.substr(5)));
        else expect(line.compare(0, 1, "#") == 0, "non-sample output is a protocol comment");
    }
    return result;
}

std::string runAt(uint32_t ms)
{
    fakeMicroseconds = static_cast<uint64_t>(ms) * 1000U;
    Serial.clear();
    loop();
    expect(SPI.responses.empty() && !SPI.transaction, "loop consumed expected retries and closed transaction");
    return Serial.str();
}

void queueFailedSample(uint32_t raw, uint8_t flags)
{
    for (unsigned i = 0; i < Config::MAX_RETRIES; ++i) SPI.responses.push_back(frame(raw, flags));
}

void testSketch()
{
    setup();
    expect(Serial.baud == 115200, "unchanged host baud");
    expect(Serial.str().find("continuous radians") != std::string::npos, "startup declares continuous output");

    // An invalid first sample must not initialize the absolute reference.
    queueFailedSample(3U * counts / 4U, 7);
    expect(values(runAt(10)).empty(), "all failed retries publish no angle");
    expect(diagnostics.dropped == 1 && diagnostics.weakField == 3 &&
        diagnostics.undervoltage == 3 && diagnostics.overspeed == 3, "all failed attempts counted by cause");

    const double reported[] = {0.01754191, 6.28291866, 6.26259341};
    for (unsigned i = 0; i < 3; ++i)
    {
        const uint32_t raw = rawFromRadians(reported[i]);
        SPI.responses.push_back(frame(raw));
        const auto received = values(runAt(20U + i * 10U));
        expect(received.size() == 1, "one sample per successful cycle");
        const double expected = raw * turn / counts - (i == 0 ? 0.0 : turn);
        near(received[0], expected, "user trace crosses zero as a small continuous angle");
        if (i > 0) expect(received[0] < 0.0 && received[0] > -0.03, "wrapped samples are small negative angles");
    }

    // Three failed quarter-turn positions would introduce a phantom turn if
    // rejected samples were allowed to update the real sketch's tracker.
    angleTracker.reset();
    SPI.responses.push_back(frame(0));
    near(values(runAt(50)).at(0), 0.0, "start phantom-turn regression");
    for (unsigned quarter = 1; quarter <= 3; ++quarter)
    {
        queueFailedSample(quarter * counts / 4U, 2);
        expect(values(runAt(50U + quarter * 10U)).empty(), "failed cycle does not repeat stale sample");
    }
    SPI.responses.push_back(frame(0));
    near(values(runAt(90)).at(0), 0.0, "failed samples do not advance continuous tracker");

    auto corrupt = frame(0);
    corrupt[5] ^= 1;
    SPI.responses.push_back(corrupt);
    SPI.responses.push_back(frame(0, 7));
    SPI.responses.push_back(frame(100));
    const auto recovered = runAt(1000);
    expect(values(recovered).size() == 1, "last retry recovery publishes exactly one angle");
    expect(recovered.find("# MT6835 diag") != std::string::npos &&
        recovered.find("crc=1") != std::string::npos &&
        recovered.find("recovered=1") != std::string::npos &&
        recovered.find("last_error=SENSOR_FAULT") != std::string::npos,
        "recovered faults still produce diagnostic evidence");
    expect(diagnostics.dropped == 4 && !diagnostics.pending, "drop count and diagnostic flush");

    SPI.responses.push_back({{0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF}});
    SPI.responses.push_back(frame(110));
    const auto limited = runAt(1010);
    expect(values(limited).size() == 1 && limited.find("# MT6835 diag") == std::string::npos,
        "diagnostics are rate limited without losing recovered sample");
    expect(diagnostics.pending && diagnostics.busErrors == 1 && diagnostics.recovered == 2,
        "rate-limited faults remain pending");
    SPI.responses.push_back(frame(120));
    // The prior diagnostic was at 1002 ms after two 1 ms retry delays.
    const auto healthy = runAt(2003);
    expect(values(healthy).size() == 1 && healthy.find("bus=1") != std::string::npos &&
        healthy.find("recovered=2") != std::string::npos &&
        healthy.find("last_error=BUS_ERROR") != std::string::npos,
        "healthy later read flushes prior recovered errors");
}
}

int main()
{
    testDriver();
    testSketch();
    std::cout << "PASS: MT6835 driver and firmware loop (" << checks << " checks)\n";
    return EXIT_SUCCESS;
}
