#include "MT7V0.h"

MT7V0::MT7V0(
	HardwareSerial &serialPort,
	int rs485DirectionPin,
	uint8_t address,
	uint16_t registerAddress
)
	: serial(&serialPort),
	  directionPin(rs485DirectionPin),
	  slaveAddress(address),
	  angleRegister(registerAddress),
	  zeroPosition(0),
	  lastStatus(MT7V0ReadStatus::Timeout),
	  lastExceptionCode(0)
{
}

void MT7V0::begin(uint32_t baud, int rxPin, int txPin)
{
	pinMode(this->directionPin, OUTPUT);
	setTransmit(false);
	this->serial->begin(baud, SERIAL_8N1, rxPin, txPin);
}

MT7V0ReadStatus MT7V0::readRawAngle(uint16_t &rawAngle)
{
	uint8_t request[8] = {
		this->slaveAddress,
		0x03,
		static_cast<uint8_t>(this->angleRegister >> 8U),
		static_cast<uint8_t>(this->angleRegister & 0xFFU),
		0x00,
		0x01,
		0x00,
		0x00
	};
	const uint16_t requestCrc = modbusCrc16(request, 6);
	request[6] = static_cast<uint8_t>(requestCrc & 0xFFU);
	request[7] = static_cast<uint8_t>(requestCrc >> 8U);

	clearReceiveBuffer();
	setTransmit(true);
	this->serial->write(request, sizeof(request));
	this->serial->flush();
	delayMicroseconds(50);
	setTransmit(false);

	uint8_t frame[MAX_FRAME_SIZE] = {};
	size_t frameLength = 0;
	size_t expectedLength = 0;
	const uint32_t startMs = millis();

	while ((millis() - startMs) < RESPONSE_TIMEOUT_MS)
	{
		while (this->serial->available() > 0)
		{
			const int value = this->serial->read();
			if (value < 0)
			{
				continue;
			}
			if (frameLength >= MAX_FRAME_SIZE)
			{
				this->lastStatus = MT7V0ReadStatus::FrameTooLong;
				return this->lastStatus;
			}

			frame[frameLength++] = static_cast<uint8_t>(value);
			if (frameLength >= 2 && (frame[1] & 0x80U) != 0U)
			{
				expectedLength = 5;
			}
			else if (frameLength >= 3)
			{
				expectedLength = static_cast<size_t>(frame[2]) + 5U;
				if (expectedLength > MAX_FRAME_SIZE)
				{
					this->lastStatus = MT7V0ReadStatus::FrameTooLong;
					return this->lastStatus;
				}
			}

			if (expectedLength != 0 && frameLength >= expectedLength)
			{
				break;
			}
		}

		if (expectedLength != 0 && frameLength >= expectedLength)
		{
			break;
		}
		delayMicroseconds(100);
	}

	if (frameLength == 0)
	{
		this->lastStatus = MT7V0ReadStatus::Timeout;
		return this->lastStatus;
	}
	if (expectedLength == 0 || frameLength < expectedLength)
	{
		this->lastStatus = MT7V0ReadStatus::IncompleteFrame;
		return this->lastStatus;
	}

	const uint16_t calculatedCrc = modbusCrc16(frame, expectedLength - 2U);
	const uint16_t receivedCrc =
		static_cast<uint16_t>(frame[expectedLength - 2U]) |
		(static_cast<uint16_t>(frame[expectedLength - 1U]) << 8U);
	if (calculatedCrc != receivedCrc)
	{
		this->lastStatus = MT7V0ReadStatus::CrcError;
		return this->lastStatus;
	}
	if (frame[0] != this->slaveAddress)
	{
		this->lastStatus = MT7V0ReadStatus::WrongAddress;
		return this->lastStatus;
	}
	if ((frame[1] & 0x80U) != 0U)
	{
		this->lastExceptionCode = frame[2];
		this->lastStatus = MT7V0ReadStatus::ModbusException;
		return this->lastStatus;
	}
	if (frame[1] != 0x03)
	{
		this->lastStatus = MT7V0ReadStatus::WrongFunction;
		return this->lastStatus;
	}
	if (frame[2] != 0x02)
	{
		this->lastStatus = MT7V0ReadStatus::WrongByteCount;
		return this->lastStatus;
	}

	rawAngle =
		(static_cast<uint16_t>(frame[3]) << 8U) |
		static_cast<uint16_t>(frame[4]);
	this->lastExceptionCode = 0;
	this->lastStatus = MT7V0ReadStatus::Ok;
	return this->lastStatus;
}

MT7V0ReadStatus MT7V0::readAngleDegrees(double &degrees)
{
	uint16_t rawAngle = 0;
	const MT7V0ReadStatus status = readRawAngle(rawAngle);
	if (status == MT7V0ReadStatus::Ok)
	{
		degrees =
			(static_cast<double>(normalize(rawAngle)) * 360.0) /
			static_cast<double>(FULL_SCALE_STEPS);
	}
	return status;
}

MT7V0ReadStatus MT7V0::readAngleRadians(double &radians)
{
	uint16_t rawAngle = 0;
	const MT7V0ReadStatus status = readRawAngle(rawAngle);
	if (status == MT7V0ReadStatus::Ok)
	{
		radians =
			(static_cast<double>(normalize(rawAngle)) * (2.0 * PI)) /
			static_cast<double>(FULL_SCALE_STEPS);
	}
	return status;
}

void MT7V0::setZeroPosition(uint16_t zero)
{
	this->zeroPosition = zero;
}

uint16_t MT7V0::getZeroPosition() const
{
	return this->zeroPosition;
}

MT7V0ReadStatus MT7V0::getLastStatus() const
{
	return this->lastStatus;
}

uint8_t MT7V0::getLastExceptionCode() const
{
	return this->lastExceptionCode;
}

String MT7V0::getDiagnostic() const
{
	String diagnostic = statusText(this->lastStatus);
	if (this->lastStatus == MT7V0ReadStatus::ModbusException)
	{
		diagnostic += " (exception=0x";
		if (this->lastExceptionCode < 0x10)
		{
			diagnostic += '0';
		}
		diagnostic += String(this->lastExceptionCode, HEX);
		diagnostic += ')';
	}
	return diagnostic;
}

const char *MT7V0::statusText(MT7V0ReadStatus status)
{
	switch (status)
	{
		case MT7V0ReadStatus::Ok: return "OK";
		case MT7V0ReadStatus::Timeout: return "TIMEOUT";
		case MT7V0ReadStatus::IncompleteFrame: return "INCOMPLETE_FRAME";
		case MT7V0ReadStatus::FrameTooLong: return "FRAME_TOO_LONG";
		case MT7V0ReadStatus::WrongAddress: return "WRONG_ADDRESS";
		case MT7V0ReadStatus::WrongFunction: return "WRONG_FUNCTION";
		case MT7V0ReadStatus::WrongByteCount: return "WRONG_BYTE_COUNT";
		case MT7V0ReadStatus::CrcError: return "CRC_ERROR";
		case MT7V0ReadStatus::ModbusException: return "MODBUS_EXCEPTION";
		default: return "UNKNOWN";
	}
}

uint16_t MT7V0::modbusCrc16(const uint8_t *data, size_t length)
{
	uint16_t crc = 0xFFFF;
	for (size_t i = 0; i < length; ++i)
	{
		crc ^= data[i];
		for (uint8_t bit = 0; bit < 8; ++bit)
		{
			crc = (crc & 1U)
				? static_cast<uint16_t>((crc >> 1U) ^ 0xA001U)
				: static_cast<uint16_t>(crc >> 1U);
		}
	}
	return crc;
}

void MT7V0::setTransmit(bool transmit)
{
	digitalWrite(this->directionPin, transmit ? HIGH : LOW);
	delayMicroseconds(30);
}

void MT7V0::clearReceiveBuffer()
{
	while (this->serial->available() > 0)
	{
		(void)this->serial->read();
	}
}

uint16_t MT7V0::normalize(uint16_t rawAngle) const
{
	return static_cast<uint16_t>(rawAngle - this->zeroPosition);
}
