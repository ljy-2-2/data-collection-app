from collections import deque
from enum import Enum, auto
from rclpy.node import Node

import numpy as np
import time


class Event(Enum):
    CLICK = auto()
    DOUBLE_CLICK = auto()
    TRIPLE_CLICK = auto()
    EPISODE_START = auto()
    EPISODE_END = auto()


class DoubleClickDetector:
    """
    Real‑time detector for 'double dips / clicks' in a gripper‑width stream.

    Call `update(width)` once per frame (or sample).  The method returns a list
    of Event values observed **during that call** – usually empty, sometimes
    ['CLICK'], ['DOUBLE_CLICK', 'EPISODE_START'], etc.
    """

    def __init__(
        self,
        click_window_sec: float = 1.0,
        cooldown_sec: float = 0.5,      # cooldown period after multi-clicks
        closed_thresh: float = np.deg2rad(10),     # ~0.17 rad
        hysteresis: float = np.deg2rad(5),         # gap between open/closed
        min_closed_sec: float = 0.02,              # ignore micro‑spikes (~2 frames at 100fps)
    ):
        self.click_window_sec = click_window_sec
        self.cooldown_sec = cooldown_sec
        self.closed_thresh = closed_thresh
        self.open_thresh = closed_thresh + hysteresis
        self.min_closed_sec = min_closed_sec

        # State
        self._last_update_time = None
        self._state = "open"            # 'open' or 'closed'
        self._closed_start = None       # timestamp of current 'closed' period
        self._recent_clicks = deque()   # timestamps where clicks finished
        self._last_multiclick = 0       # timestamp of last double/triple click
        self._is_cooldown = False       # cooldown period or not

        # Episode bookkeeping (optional, mirrors your offline routine)
        self.in_episode = False
        self._episode_start_time = None

    # ──────────────────────────────────────────────────────────────────────
    def update(self, width: float, node: Node):
        """Feed the next width sample.  Returns list[Event]."""
        current_time = time.monotonic()
        if self._last_update_time is None:
            self._last_update_time = current_time
        events = []

        # Check if we're still in cooldown period
        if current_time - self._last_multiclick < self.cooldown_sec:
            if not self._is_cooldown:
                node.get_logger().info("Entering cooldown period; ignoring clicks for %.1fs." % self.cooldown_sec)
                self._is_cooldown = True
            return events
        
        if self._is_cooldown:
            node.get_logger().info("Ended cooldown period")
            self._is_cooldown = False

        # -----------------------------------------------------------------
        # 1) Track simple open ↔︎ closed transitions (with hysteresis)
        # -----------------------------------------------------------------
        if self._state == "open":
            if width < self.closed_thresh:
                self._state = "closed"
                self._closed_start = current_time
        else:  # self._state == "closed"
            if width > self.open_thresh:
                # Only count it as a click if the gripper really stayed closed
                # at least min_closed_sec
                closed_duration = current_time - self._closed_start
                if closed_duration >= self.min_closed_sec:
                    events.append(Event.CLICK)
                    self._recent_clicks.append(current_time)
                self._state = "open"
                self._closed_start = None

        # -----------------------------------------------------------------
        # 2) Prune old clicks outside the detection window
        # -----------------------------------------------------------------

        if self._recent_clicks and (
            current_time - self._recent_clicks[0] > self.click_window_sec
        ):
            self._recent_clicks.clear()

        # -----------------------------------------------------------------
        # 3) Detect double / triple clicks
        # -----------------------------------------------------------------
        n_clicks = len(self._recent_clicks)
        if Event.CLICK in events:      # only re‑evaluate on a fresh click
            if n_clicks == 2:
                events.append(Event.DOUBLE_CLICK)
                self._last_multiclick = current_time
                # Optional episode toggling (remove if not needed)
                if not self.in_episode:
                    self.in_episode = True
                    self._episode_start_time = current_time
                    events.append(Event.EPISODE_START)
                else:
                    self.in_episode = False
                    events.append(Event.EPISODE_END)
            elif n_clicks == 3:
                events.append(Event.TRIPLE_CLICK)
                self._last_multiclick = current_time
                # A triple‑click cancels the current episode, if any
                if self.in_episode:
                    self.in_episode = False
                    events.append(Event.EPISODE_END)

        self._last_update_time = current_time
        return events

    # Convenience accessors ------------------------------------------------
    @property
    def current_time(self):
        return self._last_update_time

    @property
    def episode_start_time(self):
        return self._episode_start_time if self.in_episode else None