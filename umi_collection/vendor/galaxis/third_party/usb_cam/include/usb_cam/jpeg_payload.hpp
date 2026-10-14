// Galaxis additions, licensed under the BSD-3-Clause license in LICENSE.
#ifndef USB_CAM__JPEG_PAYLOAD_HPP_
#define USB_CAM__JPEG_PAYLOAD_HPP_

#include <cstddef>
#include <cstring>

namespace usb_cam
{
// Capacity is not a JPEG length. The caller separately bounds bytes_used by
// the dequeued kernel buffer length and rejects V4L2_BUF_FLAG_ERROR buffers.
inline size_t copy_jpeg_payload(
  const char * src, const int bytes_used, char * destination, const size_t capacity)
{
  if (!src || !destination || bytes_used < 4 ||
    static_cast<size_t>(bytes_used) > capacity ||
    static_cast<unsigned char>(src[0]) != 0xff ||
    static_cast<unsigned char>(src[1]) != 0xd8)
  {
    return 0;
  }
  // Some UVC cameras align each frame with zero bytes after JPEG's EOI.
  // Those transport padding bytes are not part of the image. Preserve every
  // byte through EOI; reject truncated frames rather than synthesizing an end.
  size_t length = static_cast<size_t>(bytes_used);
  while (length > 2 && src[length - 1] == 0) {--length;}
  if (length < 4 || static_cast<unsigned char>(src[length - 2]) != 0xff ||
    static_cast<unsigned char>(src[length - 1]) != 0xd9)
  {
    return 0;
  }
  std::memcpy(destination, src, length);
  return length;
}
}  // namespace usb_cam
#endif  // USB_CAM__JPEG_PAYLOAD_HPP_
