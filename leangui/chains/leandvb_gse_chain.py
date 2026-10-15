"""DVB-S2 GSE (Generic Stream Encapsulation) decode chain.

The other leandvb chains (leandvb_chain.py) assume the BBFRAMEs carry an
embedded MPEG-TS directly and let leandvb write that straight to its
stdout, piped into mpv. A GSE-encapsulated link doesn't work that way:
leandvb's own stdout carries nothing for it (see leansdr's
s2_deframer::handle_bbframe - a generic-stream BBFRAME only ever goes to
--fd-gse, never through the TS path), so this chain instead asks for the
raw GSE data via --fd-gse and reassembles/classifies the IP traffic inside
it ourselves (see leangui/gse/). A flow that turns out to be MPEG-TS-over-
IP is streamed into mpv same as any other chain; a flow that turns out to
be plain text is surfaced via the gse_text_chunk signal instead (the GUI
wires that to the GSE text panel next to the video panel).
"""
import os
import subprocess
import time

from PySide6.QtCore import QTimer, Signal

from .leandvb_chain import LeanDVBChain, _mpv_embed_target_id
from .registry import register_chain
from ..gse.reader import GSEReader
from ..packet_out import SRC_BBF, SRC_GSE, BbframeReader, PacketOutput, PacketOutputConfig


@register_chain
class LeanDVBGSEChain(LeanDVBChain):
    display_name = "leandvb (DVB-S2, GSE/IP data)"
    standard = "DVB-S2"

    #: (flow_key, one-line status) - forwarded from GSEReader for the GUI's
    #: flow list/manifest display.
    gse_flow_update = Signal(str, str)
    #: (flow_key, text) - forwarded from GSEReader for the GSE text panel.
    gse_text_chunk = Signal(str, str)

    def __init__(self, video_widget=None, parent=None):
        super().__init__(video_widget, parent)
        self.gse_reader_thread = None
        #: set by the GUI before start(); see packet_out.PacketOutputConfig
        self.out_config = PacketOutputConfig()
        self._output = None  # PacketOutput, created once per run (survives relaunches)
        self.bbf_reader_thread = None
        # Live sources are retried forever; this paces the relaunches.
        self._relaunch_timer = QTimer(self)
        self._relaunch_timer.setSingleShot(True)
        self._relaunch_timer.timeout.connect(self._relaunch_attempt)

    def _is_live(self):
        return self._source is not None and self._source.kind == "fifo"

    @staticmethod
    def _live_retry_delay_ms(n):
        # Immediate for the first two retries (fast recovery from a brief
        # loss), then 0.5 s doubling up to 5 s so a leandvb that dies
        # instantly can't spin the CPU. Reset by _on_lock_stable.
        return 0 if n <= 2 else min(500 * 2 ** (n - 3), 5000)

    def _build_cmd(self, source, tuning, w_fd, info_w_fd, gse_w_fd, bbf_w_fd=None):
        cmd = super()._build_cmd(source, tuning, w_fd, info_w_fd)
        cmd += ['--fd-gse', f"{gse_w_fd}"]
        if bbf_w_fd is not None:
            cmd += ['--fd-bbf', f"{bbf_w_fd}"]
            if self.out_config.l3sync_effective():
                cmd.append('--bbf-l3sync')
        return cmd

    def _start_attempt(self):
        """Same job as LeanDVBChain._start_attempt (spawn one leandvb
        attempt + arm the lock watchdog), extended with the extra GSE pipe
        and reader this chain needs, and skipping the eager mpv spawn
        (see _on_video_chunk).
        """
        self._lock_confirmed = False
        self._lock_confirm_timer.stop()
        self._attempt_num += 1
        self.debug_line.emit(
            f"DVB-S2: acquiring lock, MODCOD auto-detected from PL header (attempt {self._attempt_num})..."
        )
        if self.out_config.enabled() and self._output is None:
            try:
                self._output = PacketOutput(self.out_config)
            except OSError as e:
                self.error.emit(f"Cannot open packet output:\n{e}")
                self.stop()
                return
            self.debug_line.emit(f"Packet output: {self.out_config.describe()}")
        r_fd, w_fd = os.pipe()
        info_r_fd, info_w_fd = os.pipe()
        gse_r_fd, gse_w_fd = os.pipe()
        # Always request the BBFRAME dump: it feeds the monitor (stream type,
        # CCM/ACM, ISI, frame counts) and the 'is it really decoding' check,
        # and - when selected as the output source - the packet output.
        bbf_r_fd, bbf_w_fd = os.pipe()
        cmd = self._build_cmd(self._source, self._tuning, w_fd, info_w_fd, gse_w_fd, bbf_w_fd)
        self.debug_line.emit("leandvb command: " + " ".join(cmd))

        try:
            # No TS ever reaches leandvb's stdout in GSE mode (it's DEVNULL
            # below), so there's no raw-TS recorder here (unlike the base
            # class's _start_attempt) - mpv is instead spawned lazily, fed
            # from our own GSE reassembly, the first time a flow turns out
            # to actually be video (see _on_video_chunk), which only ever
            # happens for content that positively sniffs as TS in the first
            # place - there's no "show whatever leandvb produces" case to
            # guard against here the way there is for the base chain.
            self._spawn_leandvb(self._source, cmd, w_fd, info_w_fd,
                                 extra_pass_fds=[gse_w_fd, bbf_w_fd],
                                 stdout=subprocess.DEVNULL)
            self._spawn_readers(r_fd, info_r_fd)
            self._spawn_gse_reader(gse_r_fd)
            out = self._output if self.out_config.source == SRC_BBF else None
            self.bbf_reader_thread = BbframeReader(bbf_r_fd, out)
            self.bbf_reader_thread.status.connect(self.debug_line)
            self.bbf_reader_thread.summary.connect(self.debug_line)
            self.bbf_reader_thread.start()
        except Exception as e:
            self.error.emit(f"Failed to start leandvb:\n{str(e)}")
            self.stop()
            return

        if self.ENABLE_LOCK_WATCHDOG:
            self._attempt_deadline = time.monotonic() + self.LOCK_ACQUIRE_TIMEOUT_MS / 1000.0
            self._lock_acquire_timer.start(500)

    def _produced_real_ts_output(self):
        # The base class counts TS bytes from a recorder thread, which this
        # chain doesn't have (stdout is DEVNULL), so it always answered
        # "no output". A recorded file that reached EOF after locking was
        # then misread as a false lock and replayed from the top, up to
        # MAX_LOCK_ATTEMPTS times. Count GSE bytes instead.
        # Anything decoded counts: GSE bytes, or a BBFRAME of any stream
        # type (a TS or packetized stream never reaches --fd-gse, but it is
        # still a real decode, not a false lock).
        g, b = self.gse_reader_thread, self.bbf_reader_thread
        return (g is not None and g.bytes_in > 0) or (b is not None and b.frames_seen > 0)

    def _on_lock_failure(self):
        """A recorded file is deterministic: replaying it from the top gives
        byte-identical output (this is what produced six copies of every
        packet before). Only live sources are ever retried."""
        if self._source is not None and self._source.kind == "file":
            self._lock_acquire_timer.stop()
            self._lock_confirm_timer.stop()
            self._lock_stability_timer.stop()
            self._health_check_timer.stop()
            self._lock_confirmed = False
            self.debug_line.emit(
                "Lock lost/not achieved on a recorded file - not retrying "
                "(replaying the same samples would give identical output).")
            self.status_update.emit({"lock": False})
            return
        if self._is_live():
            # Never give up on a live signal: no attempt cap, no "exhausted"
            # state. leandvb re-detects by itself; this only restarts it
            # after a crash, a false lock, or a long stretch without lock.
            self._lock_acquire_timer.stop()
            self._lock_confirm_timer.stop()
            self._lock_stability_timer.stop()
            self._health_check_timer.stop()
            self._lock_confirmed = False
            self._lock_attempt += 1
            delay = self._live_retry_delay_ms(self._lock_attempt)
            self.debug_line.emit(
                f"Live source: lock lost/not achieved - restarting leandvb "
                f"(retry #{self._lock_attempt}, no retry limit"
                + (f", waiting {delay / 1000:.1f}s" if delay else "") + ").")
            self.status_update.emit({"lock": False})
            self._relaunch_timer.start(delay)
            return
        super()._on_lock_failure()

    def _check_process_health(self):
        # The base class treats "exited cleanly after locking" as a normal
        # end of input. For a live source that is never normal.
        if self._is_live() and self.leandvb_process is not None and self.leandvb_process.poll() is not None:
            self.debug_line.emit(
                f"leandvb exited (code {self.leandvb_process.returncode}) on a live source - restarting.")
            self._on_lock_failure()
            return
        super()._check_process_health()

    def _check_lock_acquire_deadline(self):
        # Don't wait out the full acquire timeout if leandvb already died.
        if (self._is_live() and not self._lock_confirmed and self.leandvb_process is not None
                and self.leandvb_process.poll() is not None):
            self.debug_line.emit(
                f"leandvb exited (code {self.leandvb_process.returncode}) before locking - restarting.")
            self._on_lock_failure()
            return
        super()._check_lock_acquire_deadline()

    def _relaunch_attempt(self):
        # Unlike the base class, our mpv (if running at all) isn't wired to
        # leandvb's stdout - we feed it ourselves via _on_video_chunk - so
        # there's nothing to recycle here, just the leandvb attempt itself.
        self._teardown_process()
        self._start_attempt()

    def _spawn_mpv(self):
        # The base class's lock-acquired/lock-exhausted paths both call
        # this expecting a stdout-fed live player to attach - but GSE mode
        # has no stdout stream to attach to (spawn_leandvb uses
        # stdout=DEVNULL here) and no "show whatever came out" case to
        # cover in the first place, since _on_video_chunk only ever plays
        # content that positively sniffed as TS. Make this a no-op so
        # those call sites' `if self.mpv_process is None: self._spawn_mpv()`
        # stay harmless; real GSE video playback goes through
        # _spawn_mpv_for_gse_video() instead, triggered lazily by content.
        pass

    def _on_lock_acquired(self):
        # Same bookkeeping as the base class, minus the live-mpv spawn
        # (see _spawn_mpv above for why).
        self._lock_acquire_timer.stop()
        self._lock_confirmed = True
        self._health_check_timer.start(1000)
        self._lock_stability_timer.start(self.LOCK_STABLE_MS)

    def _spawn_gse_reader(self, gse_r_fd):
        out = self._output if self.out_config.source == SRC_GSE else None
        self.gse_reader_thread = GSEReader(gse_r_fd, output=out)
        self.gse_reader_thread.flow_update.connect(self.gse_flow_update)
        self.gse_reader_thread.text_chunk.connect(self.gse_text_chunk)
        self.gse_reader_thread.video_chunk.connect(self._on_video_chunk)
        self.gse_reader_thread.debug_line.connect(self.debug_line)
        self.gse_reader_thread.start()

    def _on_video_chunk(self, data):
        if self.mpv_process is None:
            try:
                self._spawn_mpv_for_gse_video()
            except Exception as e:
                self.error.emit(f"Failed to start mpv for GSE video:\n{str(e)}")
                return
        try:
            self.mpv_process.stdin.write(data)
            self.mpv_process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # mpv exited/closed its stdin - just drop further frames

    def _spawn_mpv_for_gse_video(self):
        mpv_cmd = ['mpv', '-', '--no-terminal', '--really-quiet', '--demuxer-lavf-format=mpegts']
        if self.video_widget is not None and os.environ.get('XDG_SESSION_TYPE') != 'wayland':
            # --wid embedding only works reliably under X11 (or XWayland);
            # under native Wayland, skip it and let mpv open its own window.
            mpv_cmd.insert(1, f'--wid={_mpv_embed_target_id(self.video_widget)}')
        self.mpv_process = subprocess.Popen(mpv_cmd, stdin=subprocess.PIPE)

    def _teardown_process(self):
        # Base teardown kills leandvb first, which is what closes its end
        # of the GSE pipe and unblocks gse_reader_thread's read loop with
        # EOF. Joining the thread only after that avoids the same deadlock
        # class the base class's own readers had (see _teardown_process
        # there) - stopping the thread first would wait on a read that
        # never returns.
        super()._teardown_process()
        if self.gse_reader_thread:
            self.gse_reader_thread.stop()
            self.gse_reader_thread.wait()
            self.gse_reader_thread = None
        if self.bbf_reader_thread:
            self.bbf_reader_thread.stop()
            self.bbf_reader_thread.wait()
            self.bbf_reader_thread = None

    def stop(self):
        self._relaunch_timer.stop()  # a pending live retry must not fire after an intentional stop
        super().stop()
        # Closed only on a real stop, not on a lock-watchdog relaunch, so
        # the socket / pcap file survive _teardown_process() -> _start_attempt().
        if self._output is not None:
            self.debug_line.emit(self._output.summary())
            self._output.close()
            self._output = None
