#include <Arduino.h>
#include <SPI.h>
#include "MT6835.h"
#include "EncoderAngleTracker.h"

/*
 * ESP32-C6-WROOM-1 + MT6835 magnetic encoder
 *
 * GPIO23 (MISO) <- MT6835 MISO
 * GPIO22 (MOSI) -> MT6835 MOSI
 * GPIO21 (SCK)  -> MT6835 SCK
 * GPIO20 (CSN)  -> MT6835 CSN
 * 3V3           -> MT6835 VDD (only if the encoder board supports 3.3 V)
 * GND           -> MT6835 GND
 *
 * MT6835 uses SPI mode 3. This firmware only reads angle data; it never writes
 * registers, changes the zero point, runs calibration, or programs EEPROM.
 */

namespace Config
{
constexpr int SPI_MISO_PIN = 23;
constexpr int SPI_MOSI_PIN = 22;
constexpr int SPI_SCK_PIN = 21;
constexpr int SPI_CS_PIN = 20;
constexpr uint32_t USB_BAUD = 115200;
constexpr uint32_t ENCODER_SPI_HZ = 1000000;
constexpr uint32_t SAMPLE_INTERVAL_MS = 10;
constexpr uint32_t DIAGNOSTIC_INTERVAL_MS = 1000;
constexpr uint8_t MAX_RETRIES = 3;
// Continuous radians remove the 0 <-> 2*pi discontinuity. The first sample
// keeps the absolute reference. Set false for the original [0, 2*pi) output.
constexpr bool OUTPUT_CONTINUOUS_ANGLE = true;
}

// Set a unique side + three-digit index before flashing, e.g. L003 or R003.
static const char *DEVICE_ID = "R003";

MT6835 encoder(SPI, Config::SPI_CS_PIN, Config::ENCODER_SPI_HZ);

static EncoderAngleTracker angleTracker;

// Count every failed attempt, including faults hidden by a successful retry.
// Comment lines are ignored by the existing host protocol parser.
struct ReadDiagnostics
{
	uint32_t crcErrors = 0;
	uint32_t busErrors = 0;
	uint32_t weakField = 0;
	uint32_t undervoltage = 0;
	uint32_t overspeed = 0;
	uint32_t recovered = 0;
	uint32_t dropped = 0;
	uint32_t lastSensorRaw = 0;
	uint8_t lastSensorFlags = 0;
	bool pending = false;
	String lastError;
};
static ReadDiagnostics diagnostics;

static MT6835ReadStatus readAngleWithRetry(MT6835AngleSample &sample)
{
	MT6835ReadStatus status = MT6835ReadStatus::BusError;
	for (uint8_t attempt = 0; attempt < Config::MAX_RETRIES; ++attempt)
	{
		status = encoder.readAngle(sample);
		if (status == MT6835ReadStatus::Ok)
		{
			if (attempt > 0) ++diagnostics.recovered;
			return status;
		}
		diagnostics.pending = true;
		diagnostics.lastError = encoder.getDiagnostic();
		if (status == MT6835ReadStatus::CrcError) ++diagnostics.crcErrors;
		else if (status == MT6835ReadStatus::BusError) ++diagnostics.busErrors;
		else if (status == MT6835ReadStatus::SensorFault)
		{
			diagnostics.lastSensorRaw = sample.rawAngle;
			diagnostics.lastSensorFlags = sample.statusFlags;
			if (sample.statusFlags & MT6835::STATUS_WEAK_FIELD) ++diagnostics.weakField;
			if (sample.statusFlags & MT6835::STATUS_UNDERVOLTAGE) ++diagnostics.undervoltage;
			if (sample.statusFlags & MT6835::STATUS_OVERSPEED) ++diagnostics.overspeed;
		}
		if (attempt + 1U < Config::MAX_RETRIES) delay(1);
	}
	++diagnostics.dropped;
	return status;
}

static void reportDiagnostics(uint32_t nowMs)
{
	static uint32_t lastDiagnosticMs = 0;
	if (!diagnostics.pending ||
		(nowMs - lastDiagnosticMs) < Config::DIAGNOSTIC_INTERVAL_MS) return;
	lastDiagnosticMs = nowMs;
	diagnostics.pending = false;

	Serial.print("# MT6835 diag t_ms="); Serial.print(nowMs);
	Serial.print(" crc="); Serial.print(diagnostics.crcErrors);
	Serial.print(" bus="); Serial.print(diagnostics.busErrors);
	Serial.print(" weak_field="); Serial.print(diagnostics.weakField);
	Serial.print(" undervoltage="); Serial.print(diagnostics.undervoltage);
	Serial.print(" overspeed="); Serial.print(diagnostics.overspeed);
	Serial.print(" recovered="); Serial.print(diagnostics.recovered);
	Serial.print(" dropped="); Serial.print(diagnostics.dropped);
	Serial.print(" last_sensor_raw="); Serial.print(diagnostics.lastSensorRaw);
	Serial.print(" last_sensor_flags=0x"); Serial.print(diagnostics.lastSensorFlags, HEX);
	Serial.print(" last_error="); Serial.println(diagnostics.lastError);
}

void setup()
{
	Serial.begin(Config::USB_BAUD);
	const uint32_t waitStart = millis();
	while (!Serial && (millis() - waitStart) < 3000U)
	{
		delay(10);
	}

	encoder.begin(Config::SPI_SCK_PIN, Config::SPI_MISO_PIN, Config::SPI_MOSI_PIN);
	angleTracker.reset();

	Serial.print("# MT6835 streaming: rad, device_id=");
	Serial.println(DEVICE_ID);
	Serial.println("# SPI mode 3, 1 MHz: MISO=GPIO23 MOSI=GPIO22 SCK=GPIO21 CSN=GPIO20");
	Serial.println(Config::OUTPUT_CONTINUOUS_ANGLE
		? "# Angle output: continuous radians; first sample is absolute; turns reset on reboot"
		: "# Angle output: single-turn radians [0, 2*pi)");
}

void loop()
{
	static uint32_t lastSampleMs = 0;

	const uint32_t nowMs = millis();
	if ((nowMs - lastSampleMs) < Config::SAMPLE_INTERVAL_MS)
	{
		delay(1);
		return;
	}
	lastSampleMs = nowMs;

	MT6835AngleSample sample = {};
	const MT6835ReadStatus status = readAngleWithRetry(sample);
	if (status == MT6835ReadStatus::Ok)
	{
		// Only accepted samples may advance the continuous angle. Invalid
		// readings are neither published nor replaced with stale values.
		const double radians = Config::OUTPUT_CONTINUOUS_ANGLE
			? angleTracker.update(sample.rawAngle)
			: static_cast<double>(sample.rawAngle) * (2.0 * PI) / (1UL << 21U);
		// Keep DEVICE_ID,radians and 115200 baud. Continuous output can be
		// negative or exceed 2*pi; the existing host handles both.
		Serial.print(DEVICE_ID);
		Serial.print(',');
		Serial.println(radians, 8);
	}
	// Also report faults when a later retry succeeded.
	reportDiagnostics(millis());
}
