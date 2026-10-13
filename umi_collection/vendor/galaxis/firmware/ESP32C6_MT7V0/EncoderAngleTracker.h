#ifndef ENCODER_ANGLE_TRACKER_H
#define ENCODER_ANGLE_TRACKER_H

#include <stdint.h>

// Convert MT6835 single-turn readings into a continuous angle. The first
// sample retains the sensor's absolute reference (no automatic zeroing).
// Call update only for valid readings. Movement between accepted samples
// must be less than half a turn; a single-turn sensor cannot recover lost turns.
class EncoderAngleTracker
{
public:
    double update(uint32_t rawAngle)
    {
        if (!initialized)
        {
            accumulatedSteps = rawAngle;
            initialized = true;
        }
        else
        {
            int32_t delta = static_cast<int32_t>(rawAngle) -
                static_cast<int32_t>(previousRawAngle);
            if (delta > HALF_TURN_STEPS) delta -= FULL_TURN_STEPS;
            else if (delta < -HALF_TURN_STEPS) delta += FULL_TURN_STEPS;
            accumulatedSteps += delta;
        }
        previousRawAngle = rawAngle;
        return static_cast<double>(accumulatedSteps) * RADIANS_PER_STEP;
    }

    void reset()
    {
        initialized = false;
        previousRawAngle = 0;
        accumulatedSteps = 0;
    }

private:
    static constexpr int32_t FULL_TURN_STEPS = 1L << 21;
    static constexpr int32_t HALF_TURN_STEPS = FULL_TURN_STEPS / 2;
    static constexpr double RADIANS_PER_STEP =
        6.28318530717958647692 / FULL_TURN_STEPS;
    bool initialized = false;
    uint32_t previousRawAngle = 0;
    int64_t accumulatedSteps = 0;
};

#endif
