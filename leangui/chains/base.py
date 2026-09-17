"""Base class every decode chain plugs into.

A "chain" owns whatever external processes/threads it takes to turn a raw
IQ source into decoded output (constellation points, status telemetry, and
optionally a rendered video/data stream). The GUI only ever talks to chains
through this interface and the signals below, so it never needs to know
which protocol is actually running underneath.
"""
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal


@dataclass
class TuningParams:
    sample_rate_hz: float
    offset_hz: float
    symbol_rate_hz: float
    rolloff: float


@dataclass
class IQSource:
    """Describes where a chain should read raw IQ samples from.

    kind="file": `path` is a plain file, opened directly and piped in as
    stdin (used for recorded-file playback).
    kind="fifo": `path` is a named pipe that's opened by a shell wrapper
    inside the spawned child, so the blocking open() never happens in the
    GUI process (used for live sources like an SDR, which write into the
    fifo from a separate process/thread as samples arrive).

    sample_format is "f32" (32-bit float complex, e.g. from GNU Radio /
    recorded captures) or "s16" (16-bit signed int complex, e.g. a
    BladeRF's native SC16Q11 stream).
    """
    kind: str
    path: str
    sample_format: str = "f32"


class DecodeChain(QObject):
    #: Shown in the GUI's chain selector dropdown. Must be unique.
    display_name = "Unknown Chain"

    #: list[tuple[float, float]] of (I, Q) points to plot
    constellation_points = Signal(list)
    #: free-form {"lock": bool, "MER": "...", ...} - GUI shows recognized keys
    status_update = Signal(dict)
    #: True once the decoded TS is confirmed to be mostly CA-scrambled
    #: (see io_threads.TSRecorder._scan_scrambling) - content that will
    #: never render a picture regardless of demod success, however well
    #: leandvb locks. Only chains whose leandvb stdout carries a raw TS
    #: (LeanDVBChain and subclasses - not the GSE/MPE chains, which never
    #: route stdout through a TSRecorder) ever emit this.
    encryption_detected = Signal(bool)
    #: raw/unparsed lines surfaced for troubleshooting
    debug_line = Signal(str)
    #: unrecoverable error message, shown to the user in a dialog
    error = Signal(str)

    def __init__(self, video_widget=None, parent=None):
        super().__init__(parent)
        self.video_widget = video_widget

    def start(self, source: IQSource, tuning: TuningParams) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def is_running(self) -> bool:
        raise NotImplementedError
