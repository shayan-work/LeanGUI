"""DVB-S2 MPE (Multi-Protocol Encapsulation) decode chain via TSDuck.

leandvb_gse_chain.py implements GSE/BBFRAME extraction, which is a niche
research-grade approach. Real production leandvb deployments (e.g.
Blockstream Satellite's blocksat-cli) instead extract IP traffic the classic
way: leandvb writes an ordinary embedded MPEG-TS to stdout (same as the
plain LeanDVBChain), and that TS is piped through TSDuck's `tsp -P mpe`,
which pulls MPE-encapsulated UDP datagrams back out of it. This chain adds
that field-proven path as an additive alternative alongside the GSE one -
it does not replace it.

GSE and MPE are alternative, mutually-exclusive link-layer encapsulations:
a real transponder's data PID uses one or the other, never both. If this
chain locks (FRAMELOCK) but tsp never extracts a single MPE datagram, the
signal in front of it is very likely GSE-encapsulated instead (use the GSE
chain for that) or simply carries no IP traffic at all - see
_check_mpe_traffic, which surfaces that distinction explicitly rather than
just silently producing nothing.

tsp's mpe plugin, with no --pid given, listens on whatever PID(s) PSI/SI
signaling declares as carrying MPE, so there's nothing to hardcode there.
--redirect forces every extracted datagram to one fixed local address/port
regardless of its original embedded destination, since we don't know in
advance what destination the transponder's data PID was built with - the
tradeoff is that the original per-flow source address is lost (everything
arrives from tsp's own re-sending address), so unlike the GSE chain's
per-flow tracking, extracted content here is accumulated as a single
combined stream and sniffed/displayed as one flow (see _on_datagram).
"""
import os
import shutil
import socket
import subprocess
import time

from PySide6.QtCore import QThread, Signal

from .leandvb_chain import LeanDVBChain, _mpv_embed_target_id
from .registry import register_chain
from ..gse.flows import sniff_content

#: Fixed local port tsp's --redirect sends every extracted MPE UDP datagram
#: to. A real multi-instance deployment (more than one chain/tsp running at
#: once on the same host) would need this configurable per instance; out of
#: scope for this single-instance GUI.
MPE_UDP_PORT = 47600

#: Bytes to accumulate before running sniff_content() on them - same
#: rationale/value as GSEReader.SNIFF_AFTER_BYTES.
SNIFF_AFTER_BYTES = 2048

#: How long to wait, once locked, without a single extracted MPE datagram
#: before concluding this signal probably isn't MPE-encapsulated at all
#: (most likely GSE instead - see module docstring).
NO_TRAFFIC_WARNING_S = 8.0


class MPEUDPListener(QThread):
    """Background thread: drains the UDP socket tsp's --redirect sends
    extracted MPE datagrams to, and forwards each one's raw bytes out via
    a signal for the chain to accumulate/sniff/display.

    Follows the same run()/stop() idiom as ConstellationReader/InfoReader
    in io_threads.py: a plain flag checked each loop iteration, with a
    socket timeout standing in for their blocking-read-returns-on-EOF
    exit condition (a UDP socket has no EOF to wait on instead).
    """
    datagram_received = Signal(bytes)
    bind_failed = Signal(str)

    def __init__(self, port, parent=None):
        super().__init__(parent)
        self.port = port
        self.running = True

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        try:
            sock.bind(('127.0.0.1', self.port))
        except OSError as e:
            self.bind_failed.emit(f"[mpe] failed to bind UDP listener on port {self.port}: {e}")
            return
        try:
            while self.running:
                try:
                    data, _addr = sock.recvfrom(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                self.datagram_received.emit(data)
        finally:
            sock.close()

    def stop(self):
        self.running = False


@register_chain
class LeanDVBMPEChain(LeanDVBChain):
    display_name = "leandvb (DVB-S2, MPE/IP data via TSDuck)"
    standard = "DVB-S2"

    #: (flow_key, text) - reused from LeanDVBGSEChain's signal shape so
    #: main_window.py's GSE text panel wiring (duck-typed on this signal's
    #: presence, not on chain type) works for MPE-extracted text too.
    gse_text_chunk = Signal(str, str)

    def __init__(self, video_widget=None, parent=None):
        super().__init__(video_widget, parent)
        self.tsp_process = None
        self.mpe_listener_thread = None
        self._mpe_chunks = []
        self._mpe_bytes = 0
        self._mpe_sniffed = False
        self._mpe_kind = None
        self._first_datagram_at = None
        self._no_traffic_warned = False

    def _spawn_mpv(self):
        # Same reasoning as LeanDVBGSEChain: the base class's lock-acquired
        # / lock-exhausted paths expect a stdout-fed recorder to attach to,
        # but this chain's leandvb stdout goes straight to tsp, not through
        # a TSRecorder - there's nothing to attach here. Real MPE video (if
        # any) is spawned lazily by _on_datagram once content sniffs as TS.
        pass

    def _on_lock_acquired(self):
        self._lock_acquire_timer.stop()
        self._lock_confirmed = True
        self._health_check_timer.start(1000)
        self._lock_stability_timer.start(self.LOCK_STABLE_MS)

    def _start_attempt(self):
        """Same job as LeanDVBChain._start_attempt (spawn one leandvb
        attempt + arm the lock watchdog), extended with the tsp/MPE
        extraction pipeline instead of a stdout-fed TSRecorder+mpv.
        """
        self._lock_confirmed = False
        self._lock_confirm_timer.stop()
        self._attempt_num += 1
        self._mpe_chunks = []
        self._mpe_bytes = 0
        self._mpe_sniffed = False
        self._mpe_kind = None
        self._first_datagram_at = None
        self._no_traffic_warned = False

        if shutil.which('tsp') is None:
            self.error.emit("TSDuck (tsp) not found on PATH — install TSDuck to use MPE extraction")
            return

        _mask, _fs, label = self._current_modcod_window()
        self.debug_line.emit(f"DVB-S2: trying MODCODs {label}...")
        r_fd, w_fd = os.pipe()
        info_r_fd, info_w_fd = os.pipe()
        cmd = self._build_cmd(self._source, self._tuning, w_fd, info_w_fd)

        try:
            # leandvb's stdout carries a normal embedded MPEG-TS here (same
            # as the plain LeanDVBChain) - it goes to tsp instead of mpv.
            self._spawn_leandvb(self._source, cmd, w_fd, info_w_fd)
            self._spawn_readers(r_fd, info_r_fd)
            self._spawn_tsp()
            self._spawn_mpe_listener()
        except Exception as e:
            self.error.emit(f"Failed to start leandvb/tsp MPE chain:\n{str(e)}")
            self.stop()
            return

        if self.ENABLE_LOCK_WATCHDOG:
            self._attempt_deadline = time.monotonic() + self.LOCK_ACQUIRE_TIMEOUT_MS / 1000.0
            self._lock_acquire_timer.start(500)

    def _relaunch_attempt(self):
        self._teardown_mpe()
        self._teardown_process()
        self._start_attempt()

    def _spawn_tsp(self):
        tsp_cmd = [
            'tsp',
            '-I', 'file', '-',        # read the TS from stdin
            '-P', 'mpe',
            '--udp-forward',
            '--redirect', f'127.0.0.1:{MPE_UDP_PORT}',
            '-O', 'drop',             # the TS itself isn't needed, only the extracted UDP
        ]
        self.tsp_process = subprocess.Popen(tsp_cmd, stdin=self.leandvb_process.stdout)
        # Same idiom as LeanDVBChain._spawn_mpv(): hand the read end to tsp,
        # and close our own copy so leandvb gets SIGPIPE/EPIPE (instead of
        # blocking forever writing into a pipe nobody drains) once tsp exits
        # or is killed.
        self.leandvb_process.stdout.close()

    def _spawn_mpe_listener(self):
        self.mpe_listener_thread = MPEUDPListener(MPE_UDP_PORT)
        self.mpe_listener_thread.datagram_received.connect(self._on_datagram)
        self.mpe_listener_thread.bind_failed.connect(self.debug_line)
        self.mpe_listener_thread.start()

    def _on_datagram(self, data):
        if not data:
            return
        now = time.monotonic()
        if self._first_datagram_at is None:
            self._first_datagram_at = now
            self.debug_line.emit("[mpe] first MPE datagram received - traffic confirmed")

        if self._mpe_sniffed:
            self._emit_live_payload(data)
            return

        self._mpe_chunks.append(data)
        self._mpe_bytes += len(data)
        if self._mpe_bytes >= SNIFF_AFTER_BYTES:
            self._sniff_accumulated()

    def _sniff_accumulated(self):
        desc, ext, content = sniff_content(self._mpe_chunks)
        self._mpe_sniffed = True
        self._mpe_kind = ext
        self._mpe_chunks = []
        self.debug_line.emit(f"[mpe] identified content: {desc}")
        if ext == "ts":
            self._spawn_mpv_for_mpe_video()
            self._write_to_mpv(content)
        elif ext == "txt":
            self.gse_text_chunk.emit("mpe", content.decode("utf-8", errors="replace"))
        # other kinds (images, unidentified binary, ...) are just reported
        # via the debug_line above for now - no dedicated display for them.

    def _emit_live_payload(self, payload):
        if self._mpe_kind == "ts":
            self._write_to_mpv(payload)
        elif self._mpe_kind == "txt":
            self.gse_text_chunk.emit("mpe", payload.decode("utf-8", errors="replace"))

    def _write_to_mpv(self, data):
        if self.mpv_process is None:
            return
        try:
            self.mpv_process.stdin.write(data)
            self.mpv_process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # mpv exited/closed its stdin - just drop further frames

    def _spawn_mpv_for_mpe_video(self):
        mpv_cmd = ['mpv', '-', '--no-terminal', '--really-quiet', '--demuxer-lavf-format=mpegts']
        if self.video_widget is not None and os.environ.get('XDG_SESSION_TYPE') != 'wayland':
            mpv_cmd.insert(1, f'--wid={_mpv_embed_target_id(self.video_widget)}')
        self.mpv_process = subprocess.Popen(mpv_cmd, stdin=subprocess.PIPE)

    def _check_process_health(self):
        super()._check_process_health()
        if (
            self._lock_confirmed
            and self._first_datagram_at is None
            and not self._no_traffic_warned
            and time.monotonic() - self._attempt_deadline_start() >= NO_TRAFFIC_WARNING_S
        ):
            self._no_traffic_warned = True
            self.debug_line.emit(
                "[mpe] locked, but no MPE datagrams extracted after "
                f"{NO_TRAFFIC_WARNING_S:.0f}s - this signal is likely GSE-encapsulated "
                "(try the 'leandvb (DVB-S2, GSE/IP data)' chain instead) or carries no "
                "IP traffic on any PID."
            )

    def _attempt_deadline_start(self):
        # Approximation: attempt_deadline is set to (start time + timeout),
        # so subtracting the timeout back out recovers the start time
        # without needing a separate stored timestamp.
        return self._attempt_deadline - self.LOCK_ACQUIRE_TIMEOUT_MS / 1000.0

    def _teardown_mpe(self):
        if self.tsp_process:
            self.tsp_process.terminate()
            self.tsp_process.wait()
            self.tsp_process = None
        if self.mpe_listener_thread:
            self.mpe_listener_thread.stop()
            self.mpe_listener_thread.wait()
            self.mpe_listener_thread = None
        if self.mpv_process:
            self.mpv_process.terminate()
            self.mpv_process.wait()
            self.mpv_process = None

    def stop(self):
        self._teardown_mpe()
        super().stop()
