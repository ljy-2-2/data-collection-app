// Galaxis additions, licensed under the BSD-3-Clause license in LICENSE.
#include <array>
#include <cassert>
#include <cstring>
#include "usb_cam/jpeg_payload.hpp"

int main()
{
  const char jpeg[] = {static_cast<char>(0xff), static_cast<char>(0xd8),
    'a', 'b', static_cast<char>(0xff), static_cast<char>(0xd9)};
  std::array<char, 20> dest;
  dest.fill('x');
  assert(usb_cam::copy_jpeg_payload(jpeg, sizeof(jpeg), dest.data(), dest.size()) == 6);
  assert(std::memcmp(dest.data(), jpeg, sizeof(jpeg)) == 0);
  for (size_t i = sizeof(jpeg); i < dest.size(); ++i) {assert(dest[i] == 'x');}
  const auto old = dest;
  for (int invalid : {-1, 0, 1, 3, 21}) {
    assert(usb_cam::copy_jpeg_payload(jpeg, invalid, dest.data(), dest.size()) == 0);
    assert(dest == old);
  }
  assert(usb_cam::copy_jpeg_payload(jpeg, sizeof(jpeg), dest.data(), 5) == 0);
  assert(usb_cam::copy_jpeg_payload("abcdef", 6, dest.data(), dest.size()) == 0);
  assert(usb_cam::copy_jpeg_payload(nullptr, 6, dest.data(), dest.size()) == 0);
  assert(dest == old);
  std::array<char, 12> padded{};
  std::memcpy(padded.data(), jpeg, sizeof(jpeg));
  dest.fill('x');
  assert(usb_cam::copy_jpeg_payload(padded.data(), padded.size(), dest.data(), dest.size()) == 6);
  assert(std::memcmp(dest.data(), jpeg, sizeof(jpeg)) == 0);
  for (size_t i = 6; i < dest.size(); ++i) {assert(dest[i] == 'x');}
  assert(usb_cam::copy_jpeg_payload(jpeg, 5, dest.data(), dest.size()) == 0);
  padded[7] = 'a';
  assert(usb_cam::copy_jpeg_payload(padded.data(), padded.size(), dest.data(), dest.size()) == 0);
}
