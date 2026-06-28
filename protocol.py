"""Shared Rotor protocol constants."""
from enum import Enum
from typing import Optional


DEFAULT_KVM_PORT = 9000
DEFAULT_AUDIO_PORT = 9001

AUDIO_CHANNELS = 2
AUDIO_SAMPLERATE = 48000
AUDIO_BLOCK_FRAMES = 960
AUDIO_DTYPE = 'int16'
AUDIO_PACKET_BYTES = AUDIO_BLOCK_FRAMES * AUDIO_CHANNELS * 2


class Direction(Enum):
    RIGHT = 'right'
    LEFT = 'left'
    TOP = 'top'
    BOTTOM = 'bottom'

    @classmethod
    def from_value(cls, value: Optional[str]) -> 'Direction':
        for direction in cls:
            if direction.value == value:
                return direction
        return cls.RIGHT

    @property
    def opposite(self) -> 'Direction':
        opposites = {
            Direction.RIGHT: Direction.LEFT,
            Direction.LEFT: Direction.RIGHT,
            Direction.TOP: Direction.BOTTOM,
            Direction.BOTTOM: Direction.TOP,
        }
        return opposites[self]


class Mode(Enum):
    SERVER = 'server'
    CLIENT = 'client'

    @classmethod
    def from_value(cls, value: Optional[str]) -> 'Mode':
        for mode in cls:
            if mode.value == value:
                return mode
        return cls.SERVER


class EventType(Enum):
    KEY_DOWN = 'kd'
    KEY_UP = 'ku'
    MOUSE_MOVE = 'mm'
    MOUSE_CLICK = 'mc'
    MOUSE_SCROLL = 'ms'
    RELEASE = 'release'


class MouseButton(Enum):
    LEFT_DOWN = 'ld'
    LEFT_UP = 'lu'
    RIGHT_DOWN = 'rd'
    RIGHT_UP = 'ru'
    MIDDLE_DOWN = 'md'
    MIDDLE_UP = 'mu'
