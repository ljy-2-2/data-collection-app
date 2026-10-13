from typing import Protocol, runtime_checkable

from teleop.contracts.xr import XRFrame


class XRInputError(RuntimeError):
    """Base error for XR input startup or data failures."""


class XRInputUnavailableError(XRInputError):
    """The selected XR input backend cannot be started."""


class XRInputDataError(XRInputError):
    """The XR input backend returned malformed or unsafe data."""


@runtime_checkable
class XRInputAdapter(Protocol):
    """Common lifecycle for any headset/controller input implementation."""

    def start(self) -> None:
        ...

    def read_frame(self) -> XRFrame | None:
        ...

    def close(self) -> None:
        ...
