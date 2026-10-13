#ifndef MT7V0_H
#define MT7V0_H

#include <Arduino.h>
#include <HardwareSerial.h>

enum class MT7V0ReadStatus : uint8_t
{
	Ok,
	Timeout,
	IncompleteFrame,
	FrameTooLong,
	WrongAddress,
	WrongFunction,
	WrongByteCount,
	CrcError,
	ModbusException
};

class MT7V0
{
public:
	explicit MT7V0(
		HardwareSerial &serial,
		int directionPin,
		uint8_t slaveAddress = 0x01,
		uint16_t angleRegister = 0x0001
	);

	void begin(uint32_t baud, int rxPin, int txPin);

	MT7V0ReadStatus readRawAngle(uint16_t &rawAngle);
	MT7V0ReadStatus readAngleDegrees(double &degrees);
	MT7V0ReadStatus readAngleRadians(double &radians);

	void setZeroPosition(uint16_t zeroPosition);
	uint16_t getZeroPosition() const;

	MT7V0ReadStatus getLastStatus() const;
	uint8_t getLastExceptionCode() const;
	String getDiagnostic() const;

	static const char *statusText(MT7V0ReadStatus status);
	static uint16_t modbusCrc16(const uint8_t *data, size_t length);

private:
	static constexpr uint32_t RESPONSE_TIMEOUT_MS = 30;
	static constexpr size_t MAX_FRAME_SIZE = 16;
	static constexpr uint32_t FULL_SCALE_STEPS = 65536UL;

	HardwareSerial *serial;
	int directionPin;
	uint8_t slaveAddress;
	uint16_t angleRegister;
	uint16_t zeroPosition;
	MT7V0ReadStatus lastStatus;
	uint8_t lastExceptionCode;

	void setTransmit(bool transmit);
	void clearReceiveBuffer();
	uint16_t normalize(uint16_t rawAngle) const;
};

#endif
