#ifndef GALAXIS_TEST_SPI_H
#define GALAXIS_TEST_SPI_H

#include <array>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <stdexcept>

constexpr uint8_t MSBFIRST = 1;
constexpr uint8_t SPI_MODE3 = 3;
struct SPISettings
{
    uint32_t clock;
    uint8_t order;
    uint8_t mode;
    SPISettings(uint32_t hz, uint8_t bitOrder, uint8_t spiMode)
        : clock(hz), order(bitOrder), mode(spiMode) {}
};

class SPIClass
{
public:
    using Frame = std::array<uint8_t, 6>;
    std::deque<Frame> responses;
    std::array<int, 4> pins = {{-1, -1, -1, -1}};
    unsigned transfers = 0;
    bool transaction = false;

    void begin(int sck, int miso, int mosi, int cs) { pins = {{sck, miso, mosi, cs}}; }
    void beginTransaction(const SPISettings &settings)
    {
        if (transaction || settings.clock != 1000000U ||
            settings.order != MSBFIRST || settings.mode != SPI_MODE3)
            throw std::logic_error("unexpected SPI transaction settings");
        transaction = true;
    }
    void transferBytes(const uint8_t *tx, uint8_t *rx, size_t size)
    {
        const Frame command = {{0xA0, 0x03, 0, 0, 0, 0}};
        if (!transaction || size != command.size() || responses.empty())
            throw std::logic_error("unexpected SPI transfer or missing response");
        for (size_t i = 0; i < size; ++i)
        {
            if (tx[i] != command[i]) throw std::logic_error("incorrect MT6835 command");
            rx[i] = responses.front()[i];
        }
        responses.pop_front();
        ++transfers;
    }
    void endTransaction()
    {
        if (!transaction) throw std::logic_error("SPI transaction already ended");
        transaction = false;
    }
};
extern SPIClass SPI;

#endif
