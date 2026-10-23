"""Raw decoder for the user's official Leju section 6.5.1 screenshot.

Byte0 is Status (NOT an ID), Byte6 is fault, Byte7 is driver temperature.
No physical scaling or status-bit meaning is guessed from the screenshot.
Do not apply this format to the DM-J4310 grippers on the same bus.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LejuFeedbackRaw:
    can_id: int
    status: int
    position_code: int
    velocity_code: int
    torque_code: int
    fault_code: int
    driver_temperature_raw: int


def decode_leju_feedback(can_id, data, *, expected_can_id):
    if type(can_id) is not int or type(expected_can_id) is not int or not 0 <= can_id <= 0x7ff:
        raise ValueError('standard CAN arbitration ID required')
    if can_id != expected_can_id:
        raise ValueError('unexpected Leju arbitration ID')
    if not isinstance(data, bytes) or len(data) != 8:
        raise ValueError('Leju section 6.5.1 requires DLC 8')
    return LejuFeedbackRaw(can_id, data[0], (data[1]<<8)|data[2],
                           (data[3]<<4)|(data[4]>>4),
                           ((data[4]&15)<<8)|data[5], data[6], data[7])
