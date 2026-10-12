#include "MT6835.h"

MT6835::MT6835(SPIClass &spiBus, int csPin, uint32_t clockHz)
	: spi(&spiBus), chipSelectPin(csPin), spiClockHz(clockHz ? clockHz : 1000000UL),
	  lastStatus(MT6835ReadStatus::BusError), lastStatusFlags(0)
{
}

void MT6835::begin(int sckPin, int misoPin, int mosiPin)
{
	pinMode(this->chipSelectPin, OUTPUT);
	digitalWrite(this->chipSelectPin, HIGH);
	this->spi->begin(sckPin, misoPin, mosiPin, this->chipSelectPin);
}

MT6835ReadStatus MT6835::readAngle(MT6835AngleSample &sample)
{
	this->lastStatusFlags = 0;
	// Continuous read command: 0xA003, followed by registers 0x003--0x006.
	const uint8_t tx[] = {0xA0, 0x03, 0x00, 0x00, 0x00, 0x00};
	uint8_t rx[sizeof(tx)] = {};

	this->spi->beginTransaction(SPISettings(this->spiClockHz, MSBFIRST, SPI_MODE3));
	digitalWrite(this->chipSelectPin, LOW);
	// Datasheet p20: TL >= 100 ns, TH >= half of one SCK period.
	delayMicroseconds(1);
	this->spi->transferBytes(tx, rx, sizeof(tx));
	delayMicroseconds(500000UL / this->spiClockHz + 1U);
	digitalWrite(this->chipSelectPin, HIGH);
	this->spi->endTransaction();

	// An all-high payload is invalid (its CRC would be 0x0F).
	// Do NOT reject an all-zero payload: that is also a legitimate zero angle.
	if (rx[2] == 0xFFU && rx[3] == 0xFFU && rx[4] == 0xFFU && rx[5] == 0xFFU)
	{
		this->lastStatus = MT6835ReadStatus::BusError;
		return this->lastStatus;
	}

	const uint8_t angleAndStatus[] = {rx[2], rx[3], rx[4]};
	if (crc8(angleAndStatus, sizeof(angleAndStatus)) != rx[5])
	{
		this->lastStatus = MT6835ReadStatus::CrcError;
		return this->lastStatus;
	}

	sample.rawAngle =
		(static_cast<uint32_t>(rx[2]) << 13U) |
		(static_cast<uint32_t>(rx[3]) << 5U) |
		(static_cast<uint32_t>(rx[4]) >> 3U);
	sample.statusFlags = static_cast<uint8_t>(rx[4] & 0x07U);
	this->lastStatusFlags = sample.statusFlags;
	// A valid CRC does not guarantee a valid magnetic measurement.
	this->lastStatus = sample.statusFlags == 0
		? MT6835ReadStatus::Ok : MT6835ReadStatus::SensorFault;
	return this->lastStatus;
}

MT6835ReadStatus MT6835::readAngleRadians(double &radians, uint8_t &statusFlags)
{
	MT6835AngleSample sample = {};
	const MT6835ReadStatus status = readAngle(sample);
	if (status == MT6835ReadStatus::Ok || status == MT6835ReadStatus::SensorFault)
	{
		statusFlags = sample.statusFlags;
	}
	if (status == MT6835ReadStatus::Ok)
	{
		radians = (static_cast<double>(sample.rawAngle) * (2.0 * PI)) /
			static_cast<double>(FULL_SCALE_STEPS);
	}
	return status;
}

String MT6835::getDiagnostic() const
{
	if (this->lastStatus == MT6835ReadStatus::SensorFault)
	{
		String diagnostic = "SENSOR_FAULT";
		if (this->lastStatusFlags & STATUS_OVERSPEED) diagnostic += " OVERSPEED";
		if (this->lastStatusFlags & STATUS_WEAK_FIELD) diagnostic += " WEAK_MAGNETIC_FIELD";
		if (this->lastStatusFlags & STATUS_UNDERVOLTAGE) diagnostic += " UNDERVOLTAGE";
		return diagnostic;
	}
	switch (this->lastStatus)
	{
		case MT6835ReadStatus::Ok: return "OK";
		case MT6835ReadStatus::CrcError: return "CRC_ERROR";
		case MT6835ReadStatus::BusError: return "BUS_ERROR";
		default: return "UNKNOWN";
	}
}

uint8_t MT6835::crc8(const uint8_t *data, size_t length)
{
	// MT6835 CRC polynomial: x^8 + x^2 + x + 1 (0x07), MSB first.
	uint8_t crc = 0x00;
	for (size_t i = 0; i < length; ++i)
	{
		crc ^= data[i];
		for (uint8_t bit = 0; bit < 8; ++bit)
		{
			crc = (crc & 0x80U) ? static_cast<uint8_t>((crc << 1U) ^ 0x07U)
								 : static_cast<uint8_t>(crc << 1U);
		}
	}
	return crc;
}
