"""Generic background threads for reading line/JSON data out of a pipe fd.

These are reused by any decode chain (see chains/) that talks to an
external process over pipes - they know nothing about leandvb specifically.
"""
import os
import json
from PySide6.QtCore import QThread, Signal
import re


class ConstellationReader(QThread):
    # Signal to send parsed (I, Q) points back to the main GUI thread
    new_points_signal = Signal(list)  # Sends a batch of [i, q] points

    def __init__(self, read_fd):
        super().__init__()
        self.read_fd = read_fd
        self.running = True

    def run(self):
        with os.fdopen(self.read_fd, 'r') as f:
            batch = []
            for line in f:
                if not self.running:
                    break
                line = line.strip()
                if not line.startswith("SYMBOLS "):
                    continue
                try:
                    pairs = json.loads(line[len("SYMBOLS "):])
                    for i, q in pairs:
                        batch.append((i, q))
                        if len(batch) >= 20:
                            self.new_points_signal.emit(batch)
                            batch = []
                except (ValueError, TypeError):
                    continue

            if batch:
                self.new_points_signal.emit(batch)

    def stop(self):
        self.running = False


class InfoReader(QThread):
    new_line_signal = Signal(str)

    def __init__(self, read_fd):
        super().__init__()
        self.read_fd = read_fd
        self.running = True

    def run(self):
        with os.fdopen(self.read_fd, 'r') as f:
            for line in f:
                if not self.running:
                    break
                line = line.strip()
                if line:
                    self.new_line_signal.emit(line)

    def stop(self):
        self.running = False


#: MPEG-TS packet framing (ISO/IEC 13818-1): fixed 188-byte packets, each
#: starting with sync byte 0x47.
_TS_PACKET_SIZE = 188
_TS_SYNC_BYTE = 0x47

#: How many consecutive, sync-aligned TS packets to sample before deciding
#: whether the stream is scrambled - large enough to not be thrown off by
#: a handful of packets that happen to have their scrambling bits set
#: (e.g. transient noise right after acquiring lock), small enough to
#: reach a verdict quickly (at realistic TS bitrates this is well under a
#: second of stream). Sync-loss along the way (a byte that should be 0x47
#: isn't) resets the sample instead of counting it - see
#: TSRecorder._scan_scrambling - so a burst of not-yet-locked/flickery
#: garbage can't itself get mistaken for a real "mostly scrambled"
#: verdict.
_SCRAMBLE_SAMPLE_PACKETS = 200
#: Fraction of sampled packets with transport_scrambling_control != 0
#: (see ISO/IEC 13818-1 sec. 2.4.3.3) needed to call the stream
#: encrypted. In practice this is a near-binary signal - genuinely
#: encrypted channels sample at ~99% scrambled, clear ones at ~0% - so
#: the threshold just needs to sit safely between those, not be finely
#: tuned.
_SCRAMBLE_THRESHOLD = 0.5


class TSRecorder(QThread):
    """Drains leandvb's raw TS stdout, always saving it to a file on disk,
    and additionally forwarding each chunk live to mpv's stdin once a
    player is attached (see LeanDVBChain._on_lock_acquired /
    _on_lock_failure's exhausted branch - live playback only starts once a
    lock is confirmed or attempts are exhausted, so this thread is what
    keeps recording everything leandvb produces even before/without that,
    including flickery, momentarily-locked, or otherwise corrupted output
    that would otherwise just be discarded".

    mpv can be attached/detached at any time via set_mpv() - the thread
    itself never closes or owns that process, only writes to its stdin.

    Also inspects the transport_scrambling_control bits of each TS packet
    as it streams past (see _scan_scrambling) and emits encryption_status
    once it has enough sync-aligned packets to call it - conditional-
    access-scrambled content decodes at the physical/link layer just
    fine (leandvb has no way to know or care), but is never going to
    produce a watchable picture, so the chain surfaces that distinction
    instead of just showing a black/frozen video panel with no
    explanation (see LeanDVBChain._on_encryption_status).
    """
    #: True if the sampled packets came out mostly scrambled, False if
    #: mostly clear. Emitted (at most) once per TSRecorder instance -
    #: i.e. once per leandvb attempt, since a fresh one is spawned for
    #: each (see LeanDVBChain._spawn_recorder).
    encryption_status = Signal(bool)

    def __init__(self, stdout_stream, recording_path):
        super().__init__()
        self.stdout_stream = stdout_stream
        self.recording_path = recording_path
        self.running = True
        self._mpv_stdin = None
        self._ts_buf = b""
        self._pkt_count = 0
        self._scrambled_count = 0
        self._scramble_reported = False
        # Plain int, not behind a lock - same "good enough" idiom as
        # self.running elsewhere in this file: CPython's GIL makes a
        # single attribute read/increment atomic, and the only consumer
        # (LeanDVBChain._check_process_health, see its docstring) only
        # needs an approximate, eventually-consistent count to tell "some
        # real TS data came out" from "leandvb locked onto nothing and
        # produced zero bytes", not an exact one.
        self.bytes_written = 0

    def set_mpv_stdin(self, stdin):
        self._mpv_stdin = stdin

    def run(self):
        with open(self.recording_path, 'wb') as rec:
            while self.running:
                # read1(), not read(): BufferedReader.read(n) blocks,
                # issuing repeated underlying reads, until it has
                # accumulated the full n bytes or hit EOF - on a live TS
                # stream that can mean sitting on already-arrived bytes for
                # a long time before bytes_written ever reflects them,
                # since a single leandvb write() rarely fills 65536 bytes
                # on its own. That delay is invisible for a healthy,
                # fast-flowing decode, but directly undermines anything
                # that depends on bytes_written being prompt - see
                # LeanDVBChain._produced_real_ts_output's callers, which
                # need to tell "some real TS data came out" from "nothing
                # yet" within a bounded window, not after however long it
                # takes to fill a 64KB buffer. read1() returns as soon as
                # one underlying read call has any data, same as a real
                # streaming consumer wants.
                chunk = self.stdout_stream.read1(65536)
                if not chunk:
                    break  # leandvb exited / stdout closed
                rec.write(chunk)
                self.bytes_written += len(chunk)
                self._scan_scrambling(chunk)
                mpv_stdin = self._mpv_stdin
                if mpv_stdin is not None:
                    try:
                        mpv_stdin.write(chunk)
                        mpv_stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass  # mpv exited/closed its stdin - keep recording regardless

    def _scan_scrambling(self, chunk):
        """Feed one chunk of raw TS bytes into the running scrambling
        sample. A byte stream read off a pipe in arbitrary-sized chunks
        has no reason to land on 188-byte packet boundaries, so this
        buffers leftover bytes across calls and resyncs on the 0x47
        marker like any other TS demuxer would - see _TS_SYNC_BYTE.
        """
        if self._scramble_reported:
            return
        buf = self._ts_buf + chunk
        pos = 0
        n = len(buf)
        while pos + _TS_PACKET_SIZE <= n:
            if buf[pos] != _TS_SYNC_BYTE:
                # Lost alignment (or never had it, e.g. pre-lock noise) -
                # find the next candidate sync byte and start a fresh
                # sample rather than counting anything through the gap.
                nxt = buf.find(bytes((_TS_SYNC_BYTE,)), pos + 1)
                self._pkt_count = 0
                self._scrambled_count = 0
                if nxt == -1:
                    pos = n
                    break
                pos = nxt
                continue
            if buf[pos + 3] & 0xC0:  # transport_scrambling_control != '00'
                self._scrambled_count += 1
            self._pkt_count += 1
            pos += _TS_PACKET_SIZE
            if self._pkt_count >= _SCRAMBLE_SAMPLE_PACKETS:
                self._scramble_reported = True
                fraction = self._scrambled_count / self._pkt_count
                self.encryption_status.emit(fraction >= _SCRAMBLE_THRESHOLD)
                break
        self._ts_buf = buf[pos:]

    def stop(self):
        self.running = False


class StderrReader(QThread):
    new_line_signal = Signal(str)

    def __init__(self, stream):
        super().__init__()
        self.stream = stream
        self.running = True

    def run(self):
        for line in self.stream:
            if not self.running:
                break
            line = line.decode('utf-8', errors='replace').strip()
            line = re.sub(r'[_.C!]{3,}', '', line).strip()
            if line:
                self.new_line_signal.emit(line)

    def stop(self):
        self.running = False
