"""UDP forwarding of leandvb's --fd-gse / --fd-bbf dumps.

Framing (verified against leansdr dvbs2.h, s2_deframer::run_bbframe, at the
commit build.sh pins):
  --fd-bbf: one write() per validated BBFRAME = [0xb8 if --bbf-l3sync]
            + 10-byte BBHEADER + dfl/8 data bytes. DFL (bits) is bytes 4-5
            of the BBHEADER, so frames are self-delimiting.
  --fd-gse: only the data field (dfl/8 bytes) of each generic-stream
            BBFRAME, BBHEADER stripped - a gapless stream of concatenated
            GSE items. Each item is 2 + GSE_Length bytes (12-bit length in
            the low nibble of byte 0 + byte 1).
One GSE item / one BBFRAME is sent per UDP datagram.
"""
import os
import socket
import struct
import time
from dataclasses import dataclass

from PySide6.QtCore import QThread

MODE_OFF = "off"
MODE_GSE = "gse"
MODE_BBF = "bbframe"
MODE_PCAP = "pcap"  # reassembled GSE traffic as Ethernet frames, for Wireshark

BBH_LEN = 10
KBCH_MAX_BITS = 58192  # leandvb's own upper bound on dfl (fec_info::KBCH_MAX)


@dataclass
class UdpOutputConfig:
    mode: str = MODE_OFF
    host: str = "127.0.0.1"
    port: int = 5000
    bbf_l3sync: bool = False
    pcap_path: str = ""


class PcapWriter:
    """Classic pcap, LINKTYPE_ETHERNET. Flushed per frame so Wireshark can
    open the file while it is still being written."""

    def __init__(self, path):
        self._f = open(path, "wb")
        self._f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, 1))
        self._f.flush()

    def write(self, frame):
        t = time.time()
        sec = int(t)
        self._f.write(struct.pack("<IIII", sec, int((t - sec) * 1e6), len(frame), len(frame)) + frame)
        self._f.flush()

    def close(self):
        self._f.close()


class UdpSink:
    def __init__(self, host, port):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._addr = (host, port)

    def send(self, data):
        try:
            self._sock.sendto(data, self._addr)
        except OSError:
            pass  # a dropped datagram must never kill the reader thread

    def close(self):
        self._sock.close()


class GseItemSplitter:
    """Cuts a concatenated GSE byte stream into whole GSE items.

    Zero padding (16 zero bits) can appear in the stream; once a padding
    run starts, every zero byte up to the next non-zero byte is skipped.
    Inferred, not verified on air: padding run length parity is unknowable
    from the stream alone, and a continuation fragment (S=E=0) that starts
    right after padding would be mis-skipped - rare in practice.
    """

    def __init__(self):
        self.buf = bytearray()
        self._padding = False

    def feed(self, data):
        buf = self.buf
        buf += data
        n = len(buf)
        i = 0
        items = []
        while True:
            if self._padding:
                while i < n and buf[i] == 0:
                    i += 1
                if i == n:
                    break
                self._padding = False
            if n - i < 2:
                break
            if buf[i] == 0 and buf[i + 1] == 0:
                self._padding = True
                continue
            end = i + 2 + (((buf[i] & 0x0F) << 8) | buf[i + 1])
            if end > n:
                break
            items.append(bytes(buf[i:end]))
            i = end
        del buf[:i]
        return items


class BbframeSplitter:
    def __init__(self, l3sync=False):
        self.buf = bytearray()
        self.prefix = 1 if l3sync else 0

    def feed(self, data):
        buf = self.buf
        buf += data
        n = len(buf)
        i = 0
        items = []
        while True:
            p = i + self.prefix
            if n - p < BBH_LEN:
                break
            dfl_bits = (buf[p + 4] << 8) | buf[p + 5]
            if dfl_bits > KBCH_MAX_BITS or dfl_bits & 7:
                buf.clear()  # cannot happen from leandvb (it CRC-checks first); resync by dropping
                return items
            end = p + BBH_LEN + dfl_bits // 8
            if end > n:
                break
            items.append(bytes(buf[i:end]))
            i = end
        del buf[:i]
        return items


class BbframeUdpThread(QThread):
    """Reads leandvb's --fd-bbf pipe and forwards one BBFRAME per datagram.
    Like GSEReader, only unblocks (EOF) once leandvb exits - the chain's
    teardown kills leandvb before joining this thread."""

    def __init__(self, read_fd, sink, l3sync=False, parent=None):
        super().__init__(parent)
        self.read_fd = read_fd
        self.sink = sink
        self.l3sync = l3sync
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        splitter = BbframeSplitter(self.l3sync)
        try:
            while self.running:
                try:
                    chunk = os.read(self.read_fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                for frame in splitter.feed(chunk):
                    self.sink.send(frame)
        finally:
            try:
                os.close(self.read_fd)
            except OSError:
                pass
