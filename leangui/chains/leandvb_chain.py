"""DecodeChain implementation wrapping leandvb + mpv.

DVB-S2 (LeanDVBChain) and classic DVB-S (LeanDVBSChain) demod/decode from
either a recorded IQ file or a live fifo, piped through leandvb (physical
layer + framing) with mpv rendering the resulting MPEG-TS. DVB-S2 also
spawns ldpc_tool as leandvb's external LDPC helper; DVB-S has no LDPC layer
and uses leandvb's built-in Viterbi decoder instead.
"""
import os
import re
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QTimer

from .base import DecodeChain
from .registry import register_chain
from ..io_threads import ConstellationReader, InfoReader, StderrReader, TSRecorder

RECORDINGS_DIR = Path(__file__).resolve().parent.parent.parent / "recordings"

# leandvb / ldpc_tool live alongside the project root (this file is at
# <root>/leangui/chains/leandvb_chain.py), not necessarily the process cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

MODCOD_TABLE = {
    1: "QPSK1/4", 2: "QPSK1/3", 3: "QPSK2/5", 4: "QPSK1/2", 5: "QPSK3/5",
    6: "QPSK2/3", 7: "QPSK3/4", 8: "QPSK4/5", 9: "QPSK5/6", 10: "QPSK8/9",
    11: "QPSK9/10", 12: "8PSK3/5", 13: "8PSK2/3", 14: "8PSK3/4", 15: "8PSK5/6",
    16: "8PSK8/9", 17: "8PSK9/10", 18: "16APSK2/3", 19: "16APSK3/4",
    20: "16APSK4/5", 21: "16APSK5/6", 22: "16APSK8/9", 23: "16APSK9/10",
    24: "32APSK3/4", 25: "32APSK4/5", 26: "32APSK5/6", 27: "32APSK8/9",
    28: "32APSK9/10",
}

# Maps leandvb's --fd-info keyword to the status_update dict key the GUI
# understands (see widgets/info_panel.py).
_INFO_KEY_MAP = {
    "SS": "SS",
    "MER": "MER",
    "VBER": "BER",
    "FREQ": "offset",
    "MODCOD": "modcod",
}


def _is_integer_multiple(sample_rate_hz, symbol_rate_hz, rel_tol=1e-6):
    """True if sample_rate_hz is (to within floating-point/rounding noise)
    an exact integer multiple of symbol_rate_hz - see _build_cmd for why
    that determines whether --resample should be passed."""
    if symbol_rate_hz <= 0:
        return False
    ratio = sample_rate_hz / symbol_rate_hz
    return abs(ratio - round(ratio)) < rel_tol * max(ratio, 1.0)


def _build_modcod_windows(window_size=3):
    """Group all known MODCODs into small consecutive windows.

    Empirically (see LeanDVBChain docstring/comments below), handing
    leandvb's blind DVB-S2 scanner more than a handful of candidate
    MODCODs at once makes it fail to ever settle into a real lock - even
    on a signal that decodes perfectly, first try, every time, once the
    search space is narrowed to ~3 candidates including the right one.
    Full 28-way blind scanning isn't just slow, it's unreliable. This
    splits the full MODCOD_TABLE into small consecutive windows so the
    chain can sweep them (see _current_modcod_window/_on_lock_failure)
    instead of ever asking leandvb to search all of them simultaneously.
    """
    codes = sorted(MODCOD_TABLE)
    return [
        codes[i:i + window_size]
        for i in range(0, len(codes), window_size)
    ]


MODCOD_WINDOWS = _build_modcod_windows()


def _mpv_embed_target_id(video_widget):
    """The native window id mpv's --wid should embed into.

    Must be VideoPanelWidget.mpv_container, never the panel's own
    winId() - see video_panel.py's module docstring for why handing mpv
    the whole panel would make its reparented foreign window a sibling
    of the panel's own "encrypted" overlay label instead of contained
    strictly beneath it, breaking that overlay. Falls back to the widget
    itself for anything that doesn't expose mpv_container (e.g. a bare
    QWidget in a test), so this never raises for a widget that just
    doesn't have this concern.
    """
    return int(getattr(video_widget, "mpv_container", video_widget).winId())


@register_chain
class LeanDVBChain(DecodeChain):
    display_name = "leandvb (DVB-S2)"
    standard = "DVB-S2"

    # Lock-acquisition watchdog: if FRAMELOCK 1 hasn't shown up (and stayed
    # up - see LOCK_CONFIRM_MS below) within LOCK_ACQUIRE_TIMEOUT_MS of
    # spawning leandvb, or leandvb exits on its own after having locked
    # (its known crash-on-loss-of-lock bug), the attempt is considered
    # failed and retried from scratch up to MAX_LOCK_ATTEMPTS times.
    # LeanDVBSChain has its own bespoke code-rate sweep instead and opts
    # out via ENABLE_LOCK_WATCHDOG.
    LOCK_ACQUIRE_TIMEOUT_MS = 4000
    # leandvb's FRAMELOCK line flickers 0/1 while it's still probing
    # MODCODs/timing on a real, imperfect signal - a momentary "1" is not
    # the same as an actually-usable lock. Require FRAMELOCK to hold
    # continuously for this long before treating it as real (see
    # _lock_confirm_timer) - otherwise every flicker back to 0 would reset
    # the attempt counter to 0 via _on_lock_acquired, and a signal that
    # never truly settles would retry "attempt 1/5" forever instead of
    # ever reaching MAX_LOCK_ATTEMPTS and falling back to best-effort.
    LOCK_CONFIRM_MS = 1500
    # A confirmed lock can still turn out to be a brief false-lock artifact
    # (observed in practice: a badly-mistuned real signal can hold
    # FRAMELOCK 1 for well over LOCK_CONFIRM_MS before dropping again).
    # Resetting the failure budget the moment a lock is merely *confirmed*
    # let that kind of repeating false-lock reset _lock_attempt to 0 every
    # time, so a link that never actually settles would loop "attempt
    # 1/5" forever instead of ever accumulating towards MAX_LOCK_ATTEMPTS.
    # Only forgive prior failures once a lock has held continuously for
    # this much longer - see _on_lock_stable.
    LOCK_STABLE_MS = 6000
    # One attempt per MODCOD window (see _current_modcod_window), two full
    # passes - normal frames first, then short frames (empirically, mixing
    # both frame sizes into one window's --framesizes bitmask reproduces
    # the same never-locks failure as too many MODCODs at once - see
    # _build_modcod_windows - so frame size gets its own sweep dimension
    # instead of just being left wide open within a window). Covers every
    # standard MODCOD/frame-size combination once before falling back to
    # best-effort, same spirit as LeanDVBSChain's code-rate sweep covering
    # every DVB-S code rate once.
    MAX_LOCK_ATTEMPTS = len(MODCOD_WINDOWS) * 2
    ENABLE_LOCK_WATCHDOG = True
    # Untested starting guess - the auto-detect attempt below searches the
    # full MODCOD/frame-size space at once instead of a narrow 3-candidate
    # window, so it plausibly needs longer than LOCK_ACQUIRE_TIMEOUT_MS to
    # settle. Tune against your own signal.
    AUTO_DETECT_TIMEOUT_MS = 8000

    def __init__(self, video_widget=None, parent=None):
        super().__init__(video_widget, parent)
        self.leandvb_process = None
        self.mpv_process = None
        self.const_reader_thread = None
        self.info_reader_thread = None
        self.stderr_reader_thread = None
        self.ts_recorder_thread = None
        self._iq_file_stream = None

        self._source = None
        self._tuning = None
        self._lock_attempt = 0
        self._lock_exhausted = False
        self._lock_confirmed = False
        self._attempt_num = 0
        self._recording_path = None
        # Index into (pass 0: normal frames, pass 1: short frames) x
        # MODCOD_WINDOWS, advanced on every relaunch - see
        # _current_modcod_window and _on_lock_failure.
        self._modcod_window_idx = 0
        # DVB-S2 only: try one unrestricted attempt first, trusting leandvb
        # to read MODCOD/frame-size directly off the PL header (that's what
        # the PLSC field is for) - the narrow windowed sweep below only
        # kicks in as a fallback if that doesn't produce a real, stable
        # lock. See _on_lock_failure.
        self._auto_detect_phase = False
        # Set by _on_encryption_status once TSRecorder samples the TS as
        # mostly CA-scrambled - suppresses (re)spawning mpv for the rest
        # of this attempt, since a player can't show anything useful for
        # scrambled content regardless of how well leandvb locks.
        self._encrypted_detected = False

        # Polls (rather than a single fixed-delay shot) so that any number
        # of flicker/cancel cycles in _handle_framelock_edge - including
        # one that cancels *after* the deadline was first checked and
        # deferred for a grace period - still eventually gets re-evaluated
        # instead of leaving nothing to ever declare the attempt failed.
        self._attempt_deadline = None
        self._lock_acquire_timer = QTimer(self)
        self._lock_acquire_timer.timeout.connect(self._check_lock_acquire_deadline)

        # Armed on a FRAMELOCK 0->1 edge, cancelled on 1->0 before it fires -
        # only actually elapsing (see _on_lock_confirmed) if FRAMELOCK held
        # continuously for LOCK_CONFIRM_MS. Reused (armed the same way) to
        # confirm a *loss* of lock too, once already confirmed - see
        # _handle_info_line - so a momentary fade on an otherwise-good link
        # doesn't immediately tear everything down and retry.
        self._lock_confirm_timer = QTimer(self)
        self._lock_confirm_timer.setSingleShot(True)
        self._lock_confirm_timer.timeout.connect(self._on_lock_confirm_elapsed)

        self._lock_stability_timer = QTimer(self)
        self._lock_stability_timer.setSingleShot(True)
        self._lock_stability_timer.timeout.connect(self._on_lock_stable)

        self._health_check_timer = QTimer(self)
        self._health_check_timer.timeout.connect(self._check_process_health)

    def is_running(self):
        return self.leandvb_process is not None

    def _current_modcod_window(self):
        """(bitmask, framesizes_bitmask, label) for self._modcod_window_idx.

        framesizes is leandvb's own bitmask (1=normal, 2=short); pass 0
        sweeps every MODCOD window with normal frames, pass 1 repeats the
        same sweep with short frames, so a real signal on either frame
        size gets found within one full MAX_LOCK_ATTEMPTS cycle without
        ever asking leandvb to search both at once (see
        _build_modcod_windows for why that combination is unreliable).
        """
        pass_num, window_idx = divmod(self._modcod_window_idx, len(MODCOD_WINDOWS))
        window = MODCOD_WINDOWS[window_idx % len(MODCOD_WINDOWS)]
        mask = sum(1 << m for m in window)
        framesizes = 1 if pass_num % 2 == 0 else 2
        frame_label = "normal" if framesizes == 1 else "short"
        codes = ",".join(MODCOD_TABLE[m] for m in window)
        return mask, framesizes, f"{codes} ({frame_label} frames)"

    def _build_cmd(self, source, tuning, w_fd, info_w_fd):
        sample_format_flag = '--f32' if source.sample_format == 'f32' else '--s16'
        cmd = [
            str(PROJECT_ROOT / "leandvb"),
            '-v',
            '-d',
            sample_format_flag,
            '-f', f"{int(tuning.sample_rate_hz)}",
            '--sr', f"{int(tuning.symbol_rate_hz)}",
            # leandvb's own --help says --derotate is only for --fd-pp and
            # --tune is the real one, but the binary itself disagrees: it
            # unconditionally prints "--tune is broken, use --derotate
            # instead" the moment --tune is parsed (see leandvb.cc) - trust
            # that runtime warning over the (apparently stale) --help text.
            '--derotate', f"{int(tuning.offset_hz)}",
            '--roll-off', f"{tuning.rolloff}",
            '--standard', self.standard,
            # --drift ("track drift beyond leandvb's default safe window")
            # was here, but confirmed empirically to actively prevent lock
            # on a real capture (identical params minus --drift locked
            # within ~15s; with it, never locked even given 30s) - dropped
            # rather than kept "just in case", matching the flags a manual
            # leandvb invocation needed to actually decode this same file.
            '--fd-const', f"{w_fd}",   # Instruct leandvb to write symbols to our pipe write-end
            '--fd-info', f"{info_w_fd}",
            '--json',                   # Tells leandvb to format outputs as easy-to-parse JSON
        ]
        if not _is_integer_multiple(tuning.sample_rate_hz, tuning.symbol_rate_hz):
            # On a capture whose sample rate isn't already an exact
            # multiple of the symbol rate, leandvb never locks at all
            # without --resample (confirmed empirically: identical params
            # minus this flag never locked even given 30s, i.e. not just a
            # speed issue). But the reverse is also true and was missed
            # here for a long time: on a capture where the sample rate
            # *is* already an exact multiple (e.g. 12 MHz / 1.5 Msym/s = 8),
            # unconditionally adding --resample anyway actively prevents
            # lock - confirmed by isolating it as the one flag (out of
            # --hq/--resample/--derotate) that turned a clean, immediate
            # lock into permanent FRAMELOCK flicker on an otherwise-known-
            # good real DVB-S2 capture. So only add it when it's actually
            # needed.
            cmd.append('--resample')
        if self.standard == "DVB-S2":
            cmd += ['--hq', '--ldpc-helper', str(PROJECT_ROOT / "ldpc_tool")]
            if not self._auto_detect_phase:
                mask, framesizes, _label = self._current_modcod_window()
                cmd += ['--modcods', str(mask), '--framesizes', str(framesizes)]
        else:
            # Classic DVB-S has no LDPC layer; leandvb's built-in Viterbi
            # decoder would normally handle the convolutional code via
            # --hq/--viterbi, but that combination is confirmed empirically
            # ~1000x slower than real-time in this leandvb build for this
            # standard (measured: ~300 bytes/s of TS output with --hq vs.
            # ~2.5 MB/s without it, same file/params otherwise) - slow
            # enough that no live decode ever produces visible video before
            # a user gives up. --sampler rrc + --fastlock alone (no --hq,
            # no --viterbi) still locks and matches what a manual leandvb
            # invocation needed to actually decode this same file in
            # practice, at the cost of running without FEC correction.
            cmd += ['--sampler', 'rrc', '--fastlock']
        return cmd

    def start(self, source, tuning):
        self.stop()  # prevent double-running
        self._source = source
        self._tuning = tuning
        self._lock_attempt = 0
        self._lock_exhausted = False
        self._modcod_window_idx = 0
        self._auto_detect_phase = (self.standard == "DVB-S2")
        RECORDINGS_DIR.mkdir(exist_ok=True)
        self._start_attempt()

    def _start_attempt(self):
        """Spawn one leandvb attempt (process + recorder + readers) and arm
        the lock-acquisition watchdog. Split out from start() so a failed
        attempt can be relaunched with the same source/tuning (see
        _relaunch_attempt) without repeating start()'s one-time state
        reset. Overridden by LeanDVBGSEChain to add its gse_reader_thread.

        mpv is deliberately NOT spawned here: this attempt might turn out
        to be one of several flickery, never-actually-locking retries (see
        the confirm-timer machinery in _handle_info_line), and eagerly
        piping unlocked/garbage leandvb output straight into a live player
        is exactly the "shows corrupted video for no reason" behavior this
        was built to fix. Live playback only starts once a lock is
        genuinely confirmed (_on_lock_acquired) or all attempts are
        exhausted (_on_lock_failure) - either way, _spawn_recorder() below
        is already continuously saving everything leandvb produces to a
        file from the very start of the attempt, so nothing decoded during
        an unconfirmed/flickery period is lost even though it isn't shown
        live.
        """
        self._lock_confirmed = False
        self._lock_confirm_timer.stop()
        self._encrypted_detected = False
        self._attempt_num += 1
        if self.standard == "DVB-S2":
            if self._auto_detect_phase:
                self.debug_line.emit("DVB-S2: auto-detecting MODCOD/frame size from PL header...")
            else:
                _mask, _fs, label = self._current_modcod_window()
                self.debug_line.emit(f"DVB-S2: trying MODCODs {label}...")
        r_fd, w_fd = os.pipe()
        info_r_fd, info_w_fd = os.pipe()
        cmd = self._build_cmd(self._source, self._tuning, w_fd, info_w_fd)

        try:
            self._spawn_leandvb(self._source, cmd, w_fd, info_w_fd)
            self._spawn_recorder()
            self._spawn_readers(r_fd, info_r_fd)
        except Exception as e:
            self.error.emit(f"Failed to start leandvb:\n{str(e)}")
            self.stop()
            return

        if self.ENABLE_LOCK_WATCHDOG:
            timeout_ms = self.AUTO_DETECT_TIMEOUT_MS if self._auto_detect_phase else self.LOCK_ACQUIRE_TIMEOUT_MS
            self._attempt_deadline = time.monotonic() + timeout_ms / 1000.0
            self._lock_acquire_timer.start(500)

    def _spawn_recorder(self):
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self._recording_path = RECORDINGS_DIR / f"{timestamp}_attempt{self._attempt_num}.ts"
        self.ts_recorder_thread = TSRecorder(self.leandvb_process.stdout, self._recording_path)
        self.ts_recorder_thread.encryption_status.connect(self._on_encryption_status)
        self.ts_recorder_thread.start()

    def _on_encryption_status(self, encrypted):
        self.encryption_detected.emit(encrypted)
        if not encrypted:
            return
        self._encrypted_detected = True
        self.debug_line.emit(
            "Content is CA-scrambled (conditional access) - decode is fine, "
            "but no player can render this without descrambling keys."
        )
        if self.mpv_process is not None:
            # A frozen/black mpv window with no explanation is worse than
            # no player at all - tear it down and let the video panel's
            # own overlay (see VideoPanelWidget.set_encrypted, driven by
            # encryption_detected above) take over instead. Guarded by
            # _encrypted_detected in _on_lock_acquired/_on_lock_failure so
            # nothing respawns it afterwards for this attempt.
            self.mpv_process.terminate()
            self.mpv_process.wait()
            self.mpv_process = None

    def _relaunch_attempt(self):
        """Tear down a failed attempt and start a fresh one with the same
        source/tuning (there's no parameter to vary for DVB-S2 - this is
        just "try again", for transient glitches and for recovering from
        leandvb's crash-on-loss-of-lock bug).
        """
        self._teardown_process()
        if self.mpv_process:
            self.mpv_process.terminate()
            self.mpv_process.wait()
            self.mpv_process = None
        self._start_attempt()

    def _on_lock_acquired(self):
        """Called once FRAMELOCK has held continuously for LOCK_CONFIRM_MS
        (see _handle_info_line) - a real, usable lock rather than a
        momentary flicker. Starts live playback for the first time in this
        attempt, wired to the recorder thread that's already been running
        (and saving to disk) since the attempt began.
        """
        self._lock_acquire_timer.stop()  # deadline no longer relevant; health check takes over
        self._lock_confirmed = True
        self._health_check_timer.start(1000)
        self._lock_stability_timer.start(self.LOCK_STABLE_MS)
        if self.mpv_process is None and not self._encrypted_detected:
            self._spawn_mpv()

    def _on_lock_stable(self):
        # Only reachable if the lock held continuously all the way from
        # _on_lock_acquired to here (see _on_lock_failure, which cancels
        # this timer on any loss in between) - a real, sustained lock,
        # not just a brief false-lock artifact.
        #
        # But a continuously-held FRAMELOCK still isn't proof of a correct
        # decode by itself - see _check_process_health's false-lock branch
        # for the general explanation (PL sync doesn't depend on MODCOD, so
        # a narrow --modcods window can coincidentally validate a wrong
        # candidate). That check only runs once leandvb has *exited*, which
        # catches a short recorded-file attempt quickly but never fires at
        # all for a live/fifo source (which never EOFs) or a long file that
        # just keeps running - both would otherwise report a permanently
        # "stable" lock while producing zero real TS output forever, with
        # nothing left to ever advance the sweep to the actually-correct
        # window. Apply the same real-output check here as the other,
        # ordinarily-sufficient safety net for exactly that gap.
        if not self._produced_real_ts_output():
            self.debug_line.emit(
                "leandvb held FRAMELOCK continuously but produced no real TS output - "
                "false lock on the wrong MODCOD, not a successful decode; continuing the sweep."
            )
            self._on_lock_failure()
            return
        # Now it's safe to forgive whatever earlier failures led up to this attempt.
        self._lock_attempt = 0

    def _handle_framelock_edge(self, locked):
        """Debounce leandvb's FRAMELOCK line through _lock_confirm_timer so
        a momentary flicker (very common while it's still probing
        MODCODs/timing on a real signal) doesn't get treated the same as a
        genuinely-held lock or a genuine loss of one. Whichever direction
        we're debouncing towards is inferred from self._lock_confirmed at
        the moment the timer actually fires (see _on_lock_confirm_elapsed),
        so this same timer/method pair serves both the initial-acquire and
        the already-locked-but-fading cases.
        """
        if locked:
            if not self._lock_confirmed and not self._lock_confirm_timer.isActive():
                # 0->1 edge while still searching: start timing how long it holds.
                self._lock_confirm_timer.start(self.LOCK_CONFIRM_MS)
            elif self._lock_confirmed and self._lock_confirm_timer.isActive():
                # Recovered before a fade was confirmed as a real loss.
                self._lock_confirm_timer.stop()
        else:
            if self._lock_confirmed and not self._lock_confirm_timer.isActive():
                # 1->0 edge while genuinely locked: start timing whether
                # this is a real loss or just a brief fade.
                self._lock_confirm_timer.start(self.LOCK_CONFIRM_MS)
            elif not self._lock_confirmed and self._lock_confirm_timer.isActive():
                # Dropped back to 0 before an acquire attempt was confirmed
                # - just a flicker, not a real (failed) attempt on its own.
                self._lock_confirm_timer.stop()

    def _on_lock_confirm_elapsed(self):
        """_lock_confirm_timer only ever reaches its timeout uninterrupted
        (see _handle_framelock_edge) - i.e. FRAMELOCK held steady at
        whichever value started the timer for the full LOCK_CONFIRM_MS
        window. Which outcome that means depends on whether we were
        already confirmed-locked when it started.
        """
        if self._lock_confirmed:
            self._on_lock_failure()  # was locked, FRAMELOCK stayed 0 - real loss
        else:
            self._on_lock_acquired()  # was searching, FRAMELOCK stayed 1 - real acquire

    def _check_lock_acquire_deadline(self):
        if self._lock_confirmed or self._lock_confirm_timer.isActive():
            return  # already locked, or mid-confirmation - give it the extra grace period
        if time.monotonic() >= self._attempt_deadline:
            self._on_lock_failure()

    def _check_process_health(self):
        if self.leandvb_process is None or self.leandvb_process.poll() is None:
            return  # still running - nothing to do
        if self.leandvb_process.returncode == 0 and self._lock_confirmed:
            if self._produced_real_ts_output():
                # A confirmed-locked leandvb that then exits with a clean
                # returncode is normal end-of-file on a (typically short)
                # recorded test signal - not a failure. Confusing this
                # with a crash meant restarting from the top of the file
                # forever on any short recording, discarding a perfectly
                # good decode and eventually declaring exhaustion on a
                # signal that was working the whole time. A live/fifo
                # source (SDR) never hits this path in normal operation,
                # since its input never EOFs.
                self._health_check_timer.stop()
                self.debug_line.emit("leandvb finished decoding (end of input) - not a lock failure.")
                return
            # FRAMELOCK held continuously (a "confirmed" lock per
            # LOCK_CONFIRM_MS) but essentially no TS bytes ever came out -
            # a false lock, not a real one. This is a real, observed
            # failure mode of narrowing --modcods to a small sweep window
            # (see MODCOD_WINDOWS/_build_modcod_windows): leandvb's SOF
            # correlator finds a genuine physical-layer sync (that part
            # doesn't depend on MODCOD at all), but the PLHEADER's
            # MODCOD field decodes to a value outside this window's true
            # signal - with only ~3 candidates to check against instead
            # of the full 28, a wrong/noisy header is more likely to
            # coincidentally validate as one of them than it would
            # against the full table. Treating this as "done, success"
            # would silently strand the sweep on the wrong window
            # forever instead of moving on to the one the real signal is
            # actually in - so fall through to the same relaunch/sweep-
            # advance path a genuine lock failure takes.
            self.debug_line.emit(
                "leandvb held FRAMELOCK but produced no real TS output - false lock on the "
                "wrong MODCOD, not a successful decode; continuing the sweep."
            )
        # Exited unexpectedly (crash, or never even confirmed a lock), or
        # a false lock per above - e.g. the known upstream crash-on-loss-
        # of-lock bug, or a MODCOD-window false positive.
        self._on_lock_failure()

    def _produced_real_ts_output(self):
        # One full TS packet (188 bytes) is a deliberately low bar - the
        # point isn't to validate the stream, just to distinguish "some
        # real MPEG-TS came out" from "leandvb false-locked on the wrong
        # MODCOD and wrote nothing" (see _check_process_health). Absence
        # of a recorder thread (shouldn't happen once _lock_confirmed is
        # True, but this is a health check, not a hot path) counts as no
        # output rather than raising.
        if self.ts_recorder_thread is None:
            return False
        return self.ts_recorder_thread.bytes_written >= 188

    def _on_lock_failure(self):
        self._lock_acquire_timer.stop()
        self._lock_confirm_timer.stop()
        self._lock_stability_timer.stop()
        self._health_check_timer.stop()
        self._lock_confirmed = False

        if self.standard == "DVB-S2" and self._auto_detect_phase:
            # Direct PLS read didn't produce a real, stable lock - fall back
            # to the brute-force window sweep. Doesn't count against
            # MAX_LOCK_ATTEMPTS; this attempt was never part of that budget.
            self._auto_detect_phase = False
            self._modcod_window_idx = 0
            self.debug_line.emit(
                "DVB-S2: MODCOD auto-detect from PL header did not produce a stable lock; "
                "falling back to brute-force MODCOD sweep..."
            )
            self.status_update.emit({"lock": False})
            self._relaunch_attempt()
            return

        self._lock_attempt += 1

        if self._lock_attempt <= self.MAX_LOCK_ATTEMPTS:
            if self.standard == "DVB-S2":
                # Move to the next MODCOD window before rebuilding the
                # command (see _current_modcod_window) - each attempt
                # tries a different, narrow candidate set instead of
                # blindly repeating the exact same (unreliable, see
                # _build_modcod_windows) search.
                self._modcod_window_idx += 1
                _mask, _fs, label = self._current_modcod_window()
                self.debug_line.emit(
                    f"Reacquiring carrier lock (attempt {self._lock_attempt}/{self.MAX_LOCK_ATTEMPTS}, "
                    f"trying MODCODs {label})..."
                )
            else:
                self.debug_line.emit(
                    f"Reacquiring carrier lock (attempt {self._lock_attempt}/{self.MAX_LOCK_ATTEMPTS})..."
                )
            self.status_update.emit({"lock": False})
            self._relaunch_attempt()
        elif not self._lock_exhausted:
            # Stop trying to restart: leave leandvb/readers/recorder running
            # exactly as-is (still saving to self._recording_path), and
            # start live mpv now too - this is the best signal we're going
            # to get, so show/save it rather than silently discarding it,
            # clearly flagged as unlocked/best-effort instead of pretending
            # it's a clean decode.
            self._lock_exhausted = True
            if self.mpv_process is None and not self._encrypted_detected:
                self._spawn_mpv()
            message = (
                f"Carrier lock not achieved after {self.MAX_LOCK_ATTEMPTS} attempts; "
                "showing best-effort output - it may be corrupted/unlocked."
            )
            if self._recording_path is not None:
                message += f" Full raw capture saved to {self._recording_path}"
            self.debug_line.emit(message)
            self.status_update.emit({"lock": False, "lock_exhausted": True})

    def _spawn_leandvb(self, source, cmd, w_fd, info_w_fd, extra_pass_fds=(), stdout=subprocess.PIPE):
        pass_fds = [w_fd, info_w_fd, *extra_pass_fds]
        if source.kind == "file":
            # A plain file: open and hand it straight to leandvb's stdin.
            self._iq_file_stream = open(source.path, 'rb')
            self.leandvb_process = subprocess.Popen(
                cmd,
                stdin=self._iq_file_stream,
                stdout=stdout,  # TS stream, normally consumed by mpv
                stderr=subprocess.PIPE,
                pass_fds=pass_fds,
                cwd=str(PROJECT_ROOT),
            )
        elif source.kind == "fifo":
            # A named pipe (e.g. fed live by an SDR source): run through
            # `bash -c "... < fifo"` so the blocking open() happens in
            # the spawned shell/child, never in this GUI process.
            cmd_str = " ".join(f"'{arg}'" for arg in cmd)
            self.leandvb_process = subprocess.Popen(
                ["bash", "-c", f"exec {cmd_str} < {source.path}"],
                stdout=stdout,
                stderr=subprocess.PIPE,
                pass_fds=pass_fds,
                cwd=str(PROJECT_ROOT),
            )
        else:
            raise ValueError(f"Unknown IQSource kind: {source.kind!r}")

        os.close(w_fd)
        os.close(info_w_fd)
        for fd in extra_pass_fds:
            os.close(fd)

    def _spawn_mpv(self):
        """Start live playback, fed by the recorder thread (which is
        already draining leandvb's stdout into self._recording_path) from
        this point forward - see TSRecorder.set_mpv_stdin. Unlike the
        original design, mpv's stdin isn't wired directly to leandvb's own
        stdout pipe (the recorder thread owns reading that), so there's
        nothing here for leandvb to get a broken-pipe signal from if mpv
        exits; the recorder thread's own BrokenPipeError handling covers
        that instead.
        """
        mpv_cmd = ['mpv', '-', '--no-terminal', '--really-quiet', '--demuxer-lavf-format=mpegts']
        if self.video_widget is not None and os.environ.get('XDG_SESSION_TYPE') != 'wayland':
            # --wid embedding only works reliably under X11 (or XWayland);
            # under native Wayland, skip it and let mpv open its own window.
            mpv_cmd.insert(1, f'--wid={_mpv_embed_target_id(self.video_widget)}')
        self.mpv_process = subprocess.Popen(mpv_cmd, stdin=subprocess.PIPE)
        if self.ts_recorder_thread is not None:
            self.ts_recorder_thread.set_mpv_stdin(self.mpv_process.stdin)

    def _spawn_readers(self, r_fd, info_r_fd):
        self.const_reader_thread = ConstellationReader(r_fd)
        self.const_reader_thread.new_points_signal.connect(self.constellation_points.emit)
        self.const_reader_thread.start()

        self.info_reader_thread = InfoReader(info_r_fd)
        self.info_reader_thread.new_line_signal.connect(self._handle_info_line)
        self.info_reader_thread.start()

        self.stderr_reader_thread = StderrReader(self.leandvb_process.stderr)
        self.stderr_reader_thread.new_line_signal.connect(self._handle_stderr_line)
        self.stderr_reader_thread.start()

    def stop(self):
        # Cancel the watchdog first so it can never mistake this
        # intentional stop for an unexpected death and try to relaunch.
        self._lock_acquire_timer.stop()
        self._lock_confirm_timer.stop()
        self._lock_stability_timer.stop()
        self._health_check_timer.stop()
        self._teardown_process()
        if self.mpv_process:
            self.mpv_process.terminate()
            self.mpv_process.wait()
            self.mpv_process = None

    def _teardown_process(self):
        """Tear down everything from one leandvb attempt except mpv.

        leandvb must be killed before any reader thread is joined: each
        reader (see io_threads.py) blocks in a read loop over a pipe/stream
        leandvb holds the write end of, which only EOFs - unblocking the
        read - once leandvb actually exits. Joining a reader before that is
        an unbounded deadlock on whichever thread calls stop() (the Qt GUI
        thread, normally).

        Split out from stop() so a subclass that retries leandvb with
        different parameters (see LeanDVBSChain, LeanDVBChain's own lock
        watchdog) can kill and restart the demod process without tearing
        down/re-spawning the video player every time.
        """
        if self.leandvb_process:
            self.leandvb_process.terminate()
            self.leandvb_process.wait()
            self.leandvb_process = None

        if self._iq_file_stream:
            self._iq_file_stream.close()
            self._iq_file_stream = None

        if self.const_reader_thread:
            self.const_reader_thread.stop()
            self.const_reader_thread.wait()
            self.const_reader_thread = None

        if self.ts_recorder_thread:
            self.ts_recorder_thread.stop()
            self.ts_recorder_thread.wait()
            self.ts_recorder_thread = None

        if self.info_reader_thread:
            self.info_reader_thread.stop()
            self.info_reader_thread.wait()
            self.info_reader_thread = None

        if self.stderr_reader_thread:
            self.stderr_reader_thread.stop()
            self.stderr_reader_thread.wait()
            self.stderr_reader_thread = None

    def _handle_info_line(self, line):
        parts = line.split(maxsplit=1)
        if not parts:
            return
        keyword = parts[0]
        value = parts[1].strip() if len(parts) > 1 else ""

        if keyword == "FRAMELOCK":
            locked = value == "1"
            self.status_update.emit({"lock": locked})
            if self.ENABLE_LOCK_WATCHDOG:
                self._handle_framelock_edge(locked)
            return

        mapped_key = _INFO_KEY_MAP.get(keyword)
        if mapped_key:
            self.status_update.emit({mapped_key: value})
        else:
            # Unmapped keyword - surface it instead of guessing/discarding
            self.debug_line.emit(line)

    def _handle_stderr_line(self, line):
        m = re.match(r"Spawning LDPC helper: modcod=(\d+)", line)
        if m:
            modcod_num = int(m.group(1))
            self.status_update.emit({"modcod": MODCOD_TABLE.get(modcod_num, f"Unknown MODCOD {modcod_num}")})
        else:
            self.debug_line.emit(line)


# Standard DVB-S convolutional code rates (ETSI EN 300 421). Unlike
# DVB-S2, DVB-S has no MODCOD field telling the receiver which one is in
# use, so it has to be guessed by trying each in turn.
DVB_S_CODE_RATES = ["1/2", "2/3", "3/4", "5/6", "7/8"]


@register_chain
class LeanDVBSChain(LeanDVBChain):
    """Classic DVB-S: same leandvb-based pipeline as LeanDVBChain, plus a
    code-rate sweep. leandvb is (re)started with each candidate --cr in
    turn, giving each LOCK_TIMEOUT_MS to achieve FRAMELOCK before killing
    it and moving to the next; mpv/video playback starts once a rate
    actually locks, and the sweep isn't touched again after that (a
    momentary FRAMELOCK 0 afterwards is leandvb resyncing on its own, not
    a wrong code rate). If no rate ever locks after one full sweep, playback
    starts anyway on whichever rate was tried last, flagged as best-effort
    (see _try_next_code_rate) rather than looping the sweep forever and
    showing nothing. This is a separate, bespoke retry mechanism (it's
    hunting for the right code rate, not just retrying); the generic
    lock-acquisition watchdog from LeanDVBChain is disabled here to avoid
    the two competing over the same leandvb process.
    """
    display_name = "leandvb (DVB-S)"
    standard = "DVB-S"

    LOCK_TIMEOUT_MS = 4000
    ENABLE_LOCK_WATCHDOG = False

    def __init__(self, video_widget=None, parent=None):
        super().__init__(video_widget, parent)
        self._source = None
        self._tuning = None
        self._cr_index = 0
        self._locked = False
        self._attempts_tried = 0
        self._lock_exhausted = False
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._try_next_code_rate)

    def _build_cmd(self, source, tuning, w_fd, info_w_fd):
        cmd = super()._build_cmd(source, tuning, w_fd, info_w_fd)
        cmd += ['--cr', DVB_S_CODE_RATES[self._cr_index]]
        if source.kind == "file":
            # Keep replaying the recording for the whole lock-timeout
            # window instead of exiting at EOF partway through a trial.
            cmd += ['--loop']
        return cmd

    def start(self, source, tuning):
        self.stop()  # prevent double-running
        self._source = source
        self._tuning = tuning
        self._cr_index = 0
        self._locked = False
        self._attempts_tried = 0
        self._lock_exhausted = False
        RECORDINGS_DIR.mkdir(exist_ok=True)  # _start_attempt's recorder needs this to exist
        self._start_attempt()

    def _start_attempt(self):
        self._attempt_num += 1
        r_fd, w_fd = os.pipe()
        info_r_fd, info_w_fd = os.pipe()
        cmd = self._build_cmd(self._source, self._tuning, w_fd, info_w_fd)
        self.debug_line.emit(f"DVB-S: trying code rate {DVB_S_CODE_RATES[self._cr_index]}...")

        try:
            self._spawn_leandvb(self._source, cmd, w_fd, info_w_fd)
            # Without this, leandvb's stdout PIPE is never drained until a
            # lock is confirmed (see _spawn_mpv), which also means mpv, once
            # spawned, never actually receives anything - its stdin was
            # never wired up because there was no recorder thread to own
            # that (see LeanDVBChain._spawn_mpv). Same recorder LeanDVBChain
            # (DVB-S2) already uses; also gives DVB-S captures the same
            # always-on .ts recording DVB-S2 gets.
            self._spawn_recorder()
            self._spawn_readers(r_fd, info_r_fd)
        except Exception as e:
            self.error.emit(f"Failed to start leandvb:\n{str(e)}")
            self.stop()
            return

        self._retry_timer.start(self.LOCK_TIMEOUT_MS)

    def _try_next_code_rate(self):
        if self._locked:
            return
        self._attempts_tried += 1
        if self._attempts_tried >= len(DVB_S_CODE_RATES):
            # Every code rate has now had a full LOCK_TIMEOUT_MS turn
            # without locking. Cycling forever would show nothing forever -
            # instead, stop sweeping and treat whatever this last attempt
            # produces as the best we're going to get (same spirit as
            # LeanDVBChain._on_lock_failure's exhausted branch for DVB-S2),
            # clearly flagged as unlocked/best-effort rather than silently
            # pretending it's a clean decode.
            self._lock_exhausted = True
            self.debug_line.emit(
                f"DVB-S: no code rate locked after trying all {len(DVB_S_CODE_RATES)} "
                f"(cr={DVB_S_CODE_RATES[self._cr_index]}); showing best-effort output - "
                "it may be corrupted/unlocked."
            )
            self.status_update.emit({"lock": False, "lock_exhausted": True})
            if self.mpv_process is None:
                self._spawn_mpv()
            return
        self._teardown_process()
        self._cr_index = (self._cr_index + 1) % len(DVB_S_CODE_RATES)
        self._start_attempt()

    def _handle_info_line(self, line):
        # Classic DVB-S never emits a "FRAMELOCK" line at all - only
        # DVB-S2's demod path does (see leansdr-src's leandvb.cc: the
        # classic/Viterbi demod section only prints "LOCK %d"). The base
        # class's FRAMELOCK handling is therefore a no-op here; "LOCK" is
        # this chain's actual lock signal, so it's handled directly instead
        # of being forwarded to super() as an unmapped debug line.
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[0] == "LOCK":
            locked = parts[1].strip() == "1"
            self.status_update.emit({"lock": locked})
            if locked and not self._locked:
                self._on_locked()
            return
        super()._handle_info_line(line)

    def _on_locked(self):
        self._locked = True
        self._retry_timer.stop()
        self.debug_line.emit(f"DVB-S: locked at code rate {DVB_S_CODE_RATES[self._cr_index]}")
        if self.mpv_process is None:
            self._spawn_mpv()

    def stop(self):
        self._retry_timer.stop()
        self._locked = False
        super().stop()
