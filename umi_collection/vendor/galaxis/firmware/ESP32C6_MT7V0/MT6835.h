#ifndef MT6835_H
#define MT6835_H

#include <Arduino.h>
#include <SPI.h>

enum class MT6835ReadStatus : uint8_t
{
	Ok,
	CrcError,
	BusError,
	SensorFault
};

struct MT6835AngleSample
{
	uint32_t rawAngle;
	uint8_t statusFlags;
};

class MT6835
{
public:
	// Datasheet Rev. 1.3, page 32: register 0x005 bits 2:0.
	static constexpr uint8_t STATUS_OVERSPEED = 0x01U;
	static constexpr uint8_t STATUS_WEAK_FIELD = 0x02U;
	static constexpr uint8_t STATUS_UNDERVOLTAGE = 0x04U;

	explicit MT6835(SPIClass &spi, int chipSelectPin, uint32_t spiClockHz = 1000000UL);

	void begin(int sckPin, int misoPin, int mosiPin);
	// SensorFault fills sample for diagnostics only; CRC/bus errors leave it unchanged.
	MT6835ReadStatus readAngle(MT6835AngleSample &sample);
	// SensorFault updates statusFlags but leaves radians unchanged.
	MT6835ReadStatus readAngleRadians(double &radians, uint8_t &statusFlags);
	String getDiagnostic() const;

	static uint8_t crc8(const uint8_t *data, size_t length);

private:
	static constexpr uint32_t FULL_SCALE_STEPS = 1UL << 21U;
	SPIClass *spi;
	int chipSelectPin;
	uint32_t spiClockHz;
	MT6835ReadStatus lastStatus;
	uint8_t lastStatusFlags;
};

#endif
