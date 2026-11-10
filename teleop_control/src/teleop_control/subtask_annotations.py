"""Ordered controller annotations aligned to the frames actually saved."""
from bisect import bisect_left
from typing import Annotated
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def clean_name(value):
    if not isinstance(value, str):
        raise ValueError('名称必须为文本')
    value = value.strip()
    if not value or len(value) > 120 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('名称须为 1～120 个字符，不能包含控制字符')
    return value


Name = Annotated[str, BeforeValidator(clean_name)]


class SubtaskConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Name
    labels: list[Name] = Field(min_length=1, max_length=100)


class AnnotationCycle:
    def __init__(self, config=None):
        self.config = SubtaskConfig(**config).model_dump() if config else None
        self.events = []

    def press(self, button, timestamp_ns):
        if self.config and button in ('X', 'Y'):
            self.events.append(dict(button=button, timestamp_ns=timestamp_ns))

    def export(self, timestamps, *, clock_before=None, clock_after=None):
        if not self.config:
            return {}
        timestamps = list(timestamps)
        segments, cursor, index = [], 0, 0
        labels = self.config['labels']
        for event in self.events:
            if event['button'] == 'Y':
                index = 0  # Reset order only; preserve the pending interval.
                continue
            stamp = event['timestamp_ns']
            if clock_before is not None and clock_after is not None:
                if clock_before['boot_id'] != clock_after['boot_id']:
                    raise ValueError('Controller boot changed during annotation alignment')
                span = clock_after['pc_midpoint_ns'] - clock_before['pc_midpoint_ns']
                if span <= 0:
                    raise ValueError('Clock anchors not ordered')
                ratio = (stamp - clock_before['pc_midpoint_ns']) / span
                stamp += clock_before['offset_ns'] + round(ratio * (clock_after['offset_ns'] - clock_before['offset_ns']))
            end = bisect_left(timestamps, stamp)
            if end <= cursor:
                continue  # No saved frames: do not consume a category.
            segments.append(dict(name=labels[index], start_frame=cursor, end_frame=end,
                t_start=round((timestamps[cursor]-timestamps[0])/1e9, 6),
                t_end=round((timestamps[end-1]-timestamps[0])/1e9, 6)))
            cursor, index = end, (index+1) % len(labels)
        return dict(subtask_annotation=dict(version=1, config=self.config,
            frame_interval='[start_frame,end_frame)', events=list(self.events), segments=segments))
