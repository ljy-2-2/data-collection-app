// Build outside the Arduino sketch:
// g++ -std=c++11 -Wall -Wextra -Werror -pedantic firmware/tests/test_encoder_angle_tracker.cpp -o /tmp/test_encoder_angle_tracker
// /tmp/test_encoder_angle_tracker
#include "../ESP32C6_MT7V0/EncoderAngleTracker.h"

#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>

namespace
{
constexpr uint32_t kCountsPerTurn = 1U << 21U;
constexpr double kPi = 3.141592653589793238462643383279502884;
constexpr double kTurn = 2.0 * kPi;
constexpr double kCountRadians = kTurn / kCountsPerTurn;
int checks = 0;

void expectNear(double actual, double expected, const char *context,
                double tolerance = 1e-9)
{
    ++checks;
    if (!std::isfinite(actual) || std::fabs(actual - expected) > tolerance)
    {
        std::cerr.precision(17);
        std::cerr << "FAIL: " << context << ": expected " << expected
                  << ", got " << actual << '\n';
        std::exit(EXIT_FAILURE);
    }
}

uint32_t rawFromRadians(double radians)
{
    return static_cast<uint32_t>(std::llround(radians / kCountRadians));
}

void testInitialAbsoluteReference()
{
    EncoderAngleTracker tracker;
    expectNear(tracker.update(3U * kCountsPerTurn / 4U), 1.5 * kPi,
               "initial output retains the encoder's absolute reference");
    expectNear(tracker.update(3U * kCountsPerTurn / 4U), 1.5 * kPi,
               "a stationary encoder stays stationary");
}

void testReportedWrap()
{
    // The supplied trace crosses zero in the negative direction. The second
    // absolute value is nearly 2*pi, but the actual movement is about -0.0178 rad.
    EncoderAngleTracker tracker;
    const double before = tracker.update(rawFromRadians(0.01754191));
    const double after = tracker.update(rawFromRadians(6.28291866));
    expectNear(before, 0.01754191, "reported trace before zero", kCountRadians / 2.0);
    expectNear(after, 6.28291866 - kTurn, "reported trace after zero", kCountRadians / 2.0);
    expectNear(after - before, 6.28291866 - kTurn - 0.01754191,
               "reported jump becomes a small negative step", kCountRadians);
}

void testSingleCountCrossingsAndReversal()
{
    EncoderAngleTracker tracker;
    expectNear(tracker.update(0), 0.0, "initial exact zero is valid");
    expectNear(tracker.update(kCountsPerTurn - 1U), -kCountRadians,
               "zero to maximum is one count backwards");
    expectNear(tracker.update(kCountsPerTurn - 2U), -2.0 * kCountRadians,
               "negative motion continues below zero");
    expectNear(tracker.update(0), 0.0, "direction reversal crosses back to zero");
    expectNear(tracker.update(1), kCountRadians, "positive motion continues");

    tracker.reset();
    expectNear(tracker.update(kCountsPerTurn - 1U), kTurn - kCountRadians,
               "reset restores an initial absolute reference");
    expectNear(tracker.update(0), kTurn, "maximum to zero is one count forwards");
    expectNear(tracker.update(1), kTurn + kCountRadians,
               "positive motion continues above one turn");
}

void testJitterAtZero()
{
    EncoderAngleTracker tracker;
    const uint32_t readings[] = {kCountsPerTurn - 2U, 1U, kCountsPerTurn - 1U, 0U, 2U};
    const double expected[] = {kTurn - 2.0 * kCountRadians, kTurn + kCountRadians,
                               kTurn - kCountRadians, kTurn, kTurn + 2.0 * kCountRadians};
    for (unsigned i = 0; i < sizeof(readings) / sizeof(readings[0]); ++i)
    {
        expectNear(tracker.update(readings[i]), expected[i],
                   "boundary jitter must not accumulate spurious turns");
    }
}

void testMultipleTurnsAndCounterWidth()
{
    // 4097 turns exceed the range of a signed 32-bit accumulator in 21-bit
    // encoder counts. Quarter-turn samples have an unambiguous direction.
    constexpr uint32_t quarter = kCountsPerTurn / 4U;
    constexpr uint32_t steps = 4097U * 4U;
    EncoderAngleTracker tracker;
    expectNear(tracker.update(0), 0.0, "start positive multiple turns");
    for (uint32_t step = 1; step <= steps; ++step)
    {
        const uint32_t raw = (step % 4U) * quarter;
        expectNear(tracker.update(raw), step * (kPi / 2.0),
                   "positive multiple turns retain position beyond int32 counts");
    }
    for (uint32_t step = 1; step <= steps; ++step)
    {
        const uint32_t raw = ((4U - step % 4U) % 4U) * quarter;
        expectNear(tracker.update(raw), (steps - step) * (kPi / 2.0),
                   "reverse multiple turns back to the starting point");
    }
    for (uint32_t step = 1; step <= steps; ++step)
    {
        const uint32_t raw = ((4U - step % 4U) % 4U) * quarter;
        expectNear(tracker.update(raw), -static_cast<double>(step) * (kPi / 2.0),
                   "negative multiple turns retain position beyond int32 counts");
    }

    tracker.reset();
    expectNear(tracker.update(quarter), kPi / 2.0,
               "reset discards the accumulated turn count");
    expectNear(tracker.update(2U * quarter), kPi,
               "tracking continues normally after reset");
}

void testSamplingAmbiguity()
{
    EncoderAngleTracker tracker;
    tracker.update(0);
    // A 270-degree jump has the same pair of absolute samples as a -90-degree
    // movement. Reconstruction requires < half a turn between valid samples;
    // the tracker can only choose the shortest arc, not infer the real speed.
    expectNear(tracker.update(3U * kCountsPerTurn / 4U), -kPi / 2.0,
               "ambiguous samples use the shortest arc");
}
}

int main()
{
    testInitialAbsoluteReference();
    testReportedWrap();
    testSingleCountCrossingsAndReversal();
    testJitterAtZero();
    testMultipleTurnsAndCounterWidth();
    testSamplingAmbiguity();
    std::cout << "PASS: encoder angle tracker (" << checks << " checks)\n";
    return EXIT_SUCCESS;
}
