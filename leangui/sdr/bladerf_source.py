"""Real-time IQ source backed by bladeRF-cli.

bladeRF-cli streams raw SC16Q11 (16-bit signed int I/Q) samples into
RAW_FIFO_PATH. BladeRFSource fans those bytes out to two independent
consumers so neither can stall the other:

  1. The live spectrum display - raw byte chunks are handed off (via a
     small bounded, drop-oldest queue - stale spectrum frames are harmless
     to lose) to _SpectrumWorker, which turns them into fft_size-sized
     complex64 chunks and emits spectrum_chunk. Deliberately NOT done
     inline in the fan-out read loop - see _FanoutReader's docstring for
     why that used to matter a great deal.

  2. A decode chain - the same raw bytes are pushed onto a bounded queue
     (sized off the actual configured sample rate - see
     compute_lean_queue_maxsize) that a writer thread drains into
     LEAN_FIFO_PATH. If the decode chain falls behind (e.g. right after
     lock, when LDPC + --hq get expensive, or during a MODCOD-window
     relaunch's leandvb/ldpc_tool startup jitter), the queue fills up and
     the OLDEST pending chunk is dropped to make room - the chain sees a
     small gap in its stream instead of stalling everything upstream of
     it.

A DecodeChain reads from LEAN_FIFO_PATH by being started with
chains.base.IQSource(kind="fifo", path=source.lean_fifo_path,
sample_format=source.sample_format).
"""
import os
import queue
import select
import shutil
import subprocess
import time
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal

RAW_FIFO_PATH = "/tmp/leangui_realtime_raw.iq"
LEAN_FIFO_PATH = "/tmp/leangui_realtime_leandvb.iq"

# Every raw read from the RAW fifo is capped at this many bytes - used both
# by _FanoutReader's read() call and to translate a byte-duration buffering
# target into a chunk count for the lean queue below.
RAW_READ_CHUNK_BYTES = 65536

# s16 SC16Q11: 16-bit I + 16-bit Q per sample.
BYTES_PER_SAMPLE = 4

# How long the raw->lean fan-out queue should be able to absorb a stall
# (decode-chain backpressure, or a fresh leandvb/ldpc_tool spinning up on
# a MODCOD-window relaunch) before it starts dropping samples.
LEAN_QUEUE_TARGET_SECONDS = 2.0

# Floor on the queue's chunk count regardless of sample rate, so a very
# low configured rate still gets a sane amount of headroom.
LEAN_QUEUE_MIN_CHUNKS = 64

# How many raw chunks the spectrum leg is willing to queue up before
# dropping the oldest. Deliberately small: only the most recent chunk
# actually matters for a live display redrawn at a fixed cadence (see
# MainWindow._on_realtime_chunk), so there's no reason to let stale
# spectrum data pile up even briefly.
SPECTRUM_QUEUE_MAXLEN = 4


def compute_lean_queue_maxsize(sample_rate_hz):
    """Chunk-count bound for the raw->lean fan-out queue, sized off the
    actual configured sample rate rather than a fixed constant.

    A fixed 64-chunk cap is only ~87ms of buffering at a real 12 Msps
    capture rate (64 * 65536 bytes / (12e6 * 4 bytes/sample)) - far too
    thin in practice: an end-to-end test against a real DVB-S2 signal
    found it dropped roughly half of all incoming chunks even on an
    otherwise-idle machine, entirely from ordinary thread-scheduling
    jitter plus leandvb/ldpc_tool startup cost on every MODCOD-window
    relaunch (leandvb_chain.py's sweep relaunches every few seconds while
    still searching for lock). Sizing for LEAN_QUEUE_TARGET_SECONDS of
    continuous buffering instead gives the pipeline real headroom to
    absorb that jitter instead of corrupting the stream with drops.
    """
    target_bytes = sample_rate_hz * BYTES_PER_SAMPLE * LEAN_QUEUE_TARGET_SECONDS
    return max(LEAN_QUEUE_MIN_CHUNKS, int(target_bytes / RAW_READ_CHUNK_BYTES))


def _put_drop_oldest(q, item):
    """Push item onto q, dropping the OLDEST queued item to make room if
    q is full, rather than blocking or dropping the newest arrival."""
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass


def bladerf_cli_available() -> bool:
    return shutil.which("bladeRF-cli") is not None


@dataclass
class BladeRFConfig:
    rf_freq_hz: float
    sample_rate_hz: float
    bandwidth_hz: float
    gain_mode: str          # "agc" or "manual"
    manual_gain_db: int
    device_args: str = ""   # e.g. "*:serial=..."; blank = default device


class _FanoutReader(QThread):
    """Reads RAW_FIFO_PATH once and fans raw byte chunks out to two bounded
    queues - lean_queue (decode chain) and spectrum_queue (_SpectrumWorker).

    This thread's ONLY job is draining the FIFO as fast as possible and
    handing bytes off; no numpy conversion or Qt signal emission happens
    here. That work used to run inline in this same loop and was found (via
    an end-to-end test against a real DVB-S2 signal) to slow the drain
    enough that the decode-chain queue dropped roughly half of all incoming
    chunks even under light system load - corrupting the IQ stream with
    constant discontinuities and preventing leandvb from ever holding a
    stable lock, regardless of how good the actual signal was. Keeping this
    loop minimal is load-bearing, not just tidy.
    """

    def __init__(self, raw_fifo_path, lean_queue, spectrum_queue):
        super().__init__()
        self.raw_fifo_path = raw_fifo_path
        self.lean_queue = lean_queue
        self.spectrum_queue = spectrum_queue
        self.running = True

    def run(self):
        try:
            fd = os.open(self.raw_fifo_path, os.O_RDONLY)
        except OSError:
            return

        try:
            with os.fdopen(fd, 'rb', buffering=0) as f:
                while self.running:
                    # Poll with a timeout instead of a bare blocking read()
                    # so self.running is actually rechecked promptly once
                    # stop() is called - a plain read() here can stay
                    # blocked well past BladeRFSource.stop()'s 2s .wait(),
                    # which then destroys this still-running QThread and
                    # aborts the whole process (Qt: "QThread: Destroyed
                    # while thread is still running").
                    ready, _, _ = select.select([f], [], [], 0.2)
                    if not ready:
                        continue

                    chunk = f.read(RAW_READ_CHUNK_BYTES)
                    if not chunk:
                        break

                    _put_drop_oldest(self.lean_queue, chunk)
                    _put_drop_oldest(self.spectrum_queue, chunk)
        except (OSError, ValueError):
            pass

    def stop(self):
        self.running = False


class _SpectrumWorker(QThread):
    """Consumes raw byte chunks handed off by _FanoutReader and turns them
    into fft_size-long complex64 chunks for the live spectrum display.

    Split out from _FanoutReader specifically so this thread's numpy work
    and cross-thread Qt signal emission never compete with draining the raw
    FIFO - see _FanoutReader's docstring. spectrum_queue is small and
    drop-oldest (SPECTRUM_QUEUE_MAXLEN): losing a stale spectrum frame is
    harmless, unlike losing decode-chain bytes.
    """

    new_chunk_signal = Signal(object)

    def __init__(self, fft_size, spectrum_queue):
        super().__init__()
        self.fft_size = fft_size
        self.spectrum_queue = spectrum_queue
        self.running = True

    def run(self):
        bytes_needed = self.fft_size * 2 * 2  # int16 I + int16 Q per sample
        buf = b""

        while self.running:
            try:
                chunk = self.spectrum_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            buf += chunk
            while len(buf) >= bytes_needed:
                raw = np.frombuffer(buf[:bytes_needed], dtype=np.int16).astype(np.float32)
                buf = buf[bytes_needed:]

                # bladeRF-cli streams SC16Q11: 16-bit ints whose ADC full
                # scale is +-2048 (11 fractional bits), not +-1.0.
                # Normalizing here matches the recorded-file path (already-
                # normalized complex64), so both feed
                # compute_power_spectrum()/power_to_db() on the same scale -
                # otherwise live magnitudes read ~66 dB (20*log10(2048)) too
                # high, pushing the noise floor and signal up into positive
                # dB instead of the realistic negative dBFS a file
                # recording shows.
                raw /= 2048.0

                i_data = raw[0::2]
                q_data = raw[1::2]
                iq_complex = i_data + 1j * q_data
                self.new_chunk_signal.emit(iq_complex)

    def stop(self):
        self.running = False


class _LeanWriter(QThread):
    """Drains raw byte chunks from a bounded queue and writes them into the
    LEAN fifo for a decode chain to consume."""

    def __init__(self, lean_fifo_path, lean_queue):
        super().__init__()
        self.lean_fifo_path = lean_fifo_path
        self.lean_queue = lean_queue
        self.running = True

    def run(self):
        # Opening a FIFO for writing blocks until a reader (the decode
        # chain) opens the other end - if that never happens (chain fails
        # to start, or never gets launched at all), a plain blocking
        # os.open() here would hang forever past self.running being set
        # False, and BladeRFSource.stop()'s bounded .wait() would then
        # destroy this still-running QThread and abort the process. Poll
        # with O_NONBLOCK instead so shutdown is never stuck waiting on a
        # reader that isn't coming.
        fd = None
        while self.running and fd is None:
            try:
                fd = os.open(self.lean_fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                time.sleep(0.1)
        if fd is None:
            return
        os.set_blocking(fd, True)  # restore normal blocking writes for backpressure below

        try:
            with os.fdopen(fd, 'wb', buffering=0) as f:
                while self.running:
                    try:
                        chunk = self.lean_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    try:
                        f.write(chunk)
                    except (OSError, BrokenPipeError, ValueError):
                        break
        except (OSError, ValueError):
            pass

    def stop(self):
        self.running = False


class BladeRFSource(QObject):
    """Owns the bladeRF-cli process and FIFO plumbing for the real-time chain."""

    spectrum_chunk = Signal(object)     # complex64 ndarray, fft_size long
    status_changed = Signal(str, str)   # (text, color)
    error = Signal(str)

    sample_format = "s16"

    def __init__(self, fft_size, parent=None):
        super().__init__(parent)
        self.fft_size = fft_size
        self.lean_fifo_path = LEAN_FIFO_PATH
        self.bladerf_process = None
        self._fanout_thread = None
        self._writer_thread = None
        self._spectrum_worker = None
        self._lean_queue = None
        self._spectrum_queue = None

    def is_running(self):
        return self.bladerf_process is not None

    def start(self, config: BladeRFConfig):
        self.stop()  # prevent double-running

        if not bladerf_cli_available():
            self.error.emit(
                "bladeRF-cli was not found on PATH.\n"
                "Install the BladeRF host tools and try again."
            )
            return

        # (Re)create the two FIFOs used to stream samples:
        #   RAW  <- bladeRF-cli writes here; our fan-out thread reads it
        #   LEAN <- our writer thread writes here; the decode chain reads it
        try:
            for path in (RAW_FIFO_PATH, LEAN_FIFO_PATH):
                if os.path.exists(path):
                    os.remove(path)
                os.mkfifo(path)
        except Exception as e:
            self.error.emit(f"Failed to create real-time FIFOs:\n{str(e)}")
            return

        bladerf_args = ["-d", config.device_args] if config.device_args else []
        exec_lines = [
            f"set frequency {int(config.rf_freq_hz)}",
            f"set samplerate {int(config.sample_rate_hz)}",
            f"set bandwidth {int(config.bandwidth_hz)}",
        ]
        if config.gain_mode == "agc":
            exec_lines.append("set agc rx on")
        else:
            exec_lines.append("set agc rx off")
            exec_lines.append(f"set gain rx {int(config.manual_gain_db)}")
        exec_lines.append(f"rx config file={RAW_FIFO_PATH} format=bin n=0")
        exec_lines.append("rx start")
        exec_lines.append("rx wait")

        bladerf_cmd = ["bladeRF-cli"] + bladerf_args
        for line in exec_lines:
            bladerf_cmd += ["-e", line]

        try:
            # Start the writer thread first (opens LEAN for writing, a
            # blocking open, safe here since it's a background thread) and
            # the fan-out reader (opens RAW for reading, likewise blocking)
            # so both are ready before bladeRF-cli's own writer-side open()
            # happens.
            self._lean_queue = queue.Queue(maxsize=compute_lean_queue_maxsize(config.sample_rate_hz))
            self._spectrum_queue = queue.Queue(maxsize=SPECTRUM_QUEUE_MAXLEN)

            self._writer_thread = _LeanWriter(LEAN_FIFO_PATH, self._lean_queue)
            self._writer_thread.start()

            self._spectrum_worker = _SpectrumWorker(self.fft_size, self._spectrum_queue)
            self._spectrum_worker.new_chunk_signal.connect(self.spectrum_chunk.emit)
            self._spectrum_worker.start()

            self._fanout_thread = _FanoutReader(RAW_FIFO_PATH, self._lean_queue, self._spectrum_queue)
            self._fanout_thread.start()

            self.bladerf_process = subprocess.Popen(
                bladerf_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            self.status_changed.emit(
                f"Streaming: {config.rf_freq_hz/1e6:.3f} MHz, {config.sample_rate_hz/1e6:.3f} Msps",
                "lightgreen",
            )
        except Exception as e:
            self.error.emit(f"Failed to start real-time chain:\n{str(e)}")
            self.status_changed.emit("Failed to start.", "red")
            self.stop()

    def stop(self):
        # Stop bladeRF-cli first (if running), then the fan-out reader, then
        # the spectrum worker and the writer thread, so each downstream
        # consumer hits a clean EOF/idle state instead of hanging.
        if self.bladerf_process:
            self.bladerf_process.terminate()
            try:
                self.bladerf_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.bladerf_process.kill()
                self.bladerf_process.wait()
            self.bladerf_process = None

        # Both threads now poll self.running (via select()/O_NONBLOCK open
        # retries, see _FanoutReader/_LeanWriter) instead of parking in an
        # uninterruptible blocking call, so these waits should resolve well
        # under their bound in practice - the bound itself just stays as a
        # safety net against dropping the last reference to a still-running
        # QThread, which Qt treats as fatal (aborts the process).
        if self._fanout_thread:
            self._fanout_thread.stop()
            self._fanout_thread.wait(2000)
            self._fanout_thread = None

        if self._spectrum_worker:
            self._spectrum_worker.stop()
            self._spectrum_worker.wait(2000)
            self._spectrum_worker = None

        if self._writer_thread:
            self._writer_thread.stop()
            self._writer_thread.wait(2000)
            self._writer_thread = None

        self._lean_queue = None
        self._spectrum_queue = None

        for path in (RAW_FIFO_PATH, LEAN_FIFO_PATH):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

        self.status_changed.emit("Idle.", "orange")
