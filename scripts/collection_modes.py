"""Robot choices shared by package metadata and the desktop's selection guards."""

ROBOT_NAMES = {
    "tianji": "天机 Marvin M6",
    "kuavo-5w": "乐聚 KUAVO 5-W",
    "agibot-g2": "智元 G2",
}
MODE_ROBOTS = {
    "all": tuple(ROBOT_NAMES),
    "umi": (),
    "umi-tianji": ("tianji",),  # Older packages without the KUAVO client.
    "umi-tianji-kuavo": ("tianji", "kuavo-5w"),
}


def default_collection_mode(deb_arch: str) -> str:
    # G2's bundled native SDK is amd64-only. KUAVO uses the robot's network
    # service and the PICO SDK, which is included on both architectures.
    return {"amd64": "all", "arm64": "umi-tianji-kuavo"}[deb_arch]


def robot_keys(mode: str) -> tuple[str, ...]:
    return MODE_ROBOTS.get(mode, ())
