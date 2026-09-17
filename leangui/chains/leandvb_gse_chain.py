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

from PySide6.QtCore import Signal

from .leandvb_chain import LeanDVBChain, _mpv_embed_target_id
from .registry import register_chain
from ..gse.reader import GSEReader


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

    def _build_cmd(self, source, tuning, w_fd, info_w_fd, gse_w_fd):
        cmd = super()._build_cmd(source, tuning, w_fd, info_w_fd)
        cmd += ['--fd-gse', f"{gse_w_fd}"]
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
        _mask, _fs, label = self._current_modcod_window()
        self.debug_line.emit(f"DVB-S2: trying MODCODs {label}...")
        r_fd, w_fd = os.pipe()
        info_r_fd, info_w_fd = os.pipe()
        gse_r_fd, gse_w_fd = os.pipe()
        cmd = self._build_cmd(self._source, self._tuning, w_fd, info_w_fd, gse_w_fd)

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
                                 extra_pass_fds=[gse_w_fd], stdout=subprocess.DEVNULL)
            self._spawn_readers(r_fd, info_r_fd)
            self._spawn_gse_reader(gse_r_fd)
        except Exception as e:
            self.error.emit(f"Failed to start leandvb:\n{str(e)}")
            self.stop()
            return

        if self.ENABLE_LOCK_WATCHDOG:
            self._attempt_deadline = time.monotonic() + self.LOCK_ACQUIRE_TIMEOUT_MS / 1000.0
            self._lock_acquire_timer.start(500)

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
        self.gse_reader_thread = GSEReader(gse_r_fd)
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
