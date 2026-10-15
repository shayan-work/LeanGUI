"""Packet output for the GSE/IP data chain: UDP and/or PCAP, from either
leandvb's --fd-gse stream or its --fd-bbf BBFRAME dump.

Two sources (which leandvb fd feeds the output):
  gse     --fd-gse: a gapless stream of concatenated GSE items (BBHEADER
          already stripped by leandvb).
  bbframe --fd-bbf: one write() per CRC-valid BBFRAME = [0xb8 if
          --bbf-l3sync] + 10-byte BBHEADER + dfl/8 data bytes (verified in
          leansdr dvbs2.h, s2_deframer::run_bbframe). DFL (bits) is bytes
          4-5 of the BBHEADER, so frames are self-delimiting, and stream
          type is the top 2 bits of byte 0 (1 = generic continuous = GSE).

Three levels (what each datagram / pcap record contains):
  raw  the thing leandvb wrote: one GSE item, or one BBFRAME.
  pdu  de-encapsulated: only the reassembled PDU payload (e.g. the IP
       packet), GSE headers and the label / protocol type removed.
  eth  re-encapsulated: the PDU wrapped in a synthesized 14-byte Ethernet
       header (dst = GSE label, src = zeros, ethertype = GSE protocol
       type), which is the form Wireshark opens natively.

PCAP link types per level: raw -> USER0 (147), pdu -> RAW IP (101, only
IPv4/IPv6 PDUs are written), eth -> Ethernet (1).
"""
import os
import socket
import struct
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

from PySide6.QtCore import QThread, Signal

from .gse import decap as gd

SRC_GSE = "gse"
SRC_BBF = "bbframe"
LVL_RAW = "raw"
LVL_PDU = "pdu"
LVL_ETH = "eth"

LINKTYPE = {LVL_RAW: 147, LVL_PDU: 101, LVL_ETH: 1}
BBH_LEN = 10
KBCH_MAX_BITS = 58192  # leandvb's own upper bound on dfl (fec_info::KBCH_MAX)
STREAM_TYPE_NAMES = {0: "GP", 1: "GC(GSE)", 2: "reserved", 3: "TS"}


@dataclass
class PacketOutputConfig:
    source: str = SRC_GSE
    level: str = LVL_ETH
    udp: bool = False
    host: str = "127.0.0.1"
    port: int = 5000
    pcap: bool = False
    pcap_path: str = ""
    bbf_l3sync: bool = False  # prefix 0xB8 to each BBFRAME (raw level only)

    def enabled(self):
        return self.udp or self.pcap

    def l3sync_effective(self):
        return self.bbf_l3sync and self.source == SRC_BBF and self.level == LVL_RAW

    def describe(self):
        dests = []
        if self.udp:
            dests.append(f"UDP {self.host}:{self.port}")
        if self.pcap:
            dests.append(f"PCAP {self.pcap_path}")
        return f"source={self.source} level={self.level} -> " + (", ".join(dests) or "off")


class PcapWriter:
    """Classic pcap. Flushed per record so Wireshark can open the file
    while it is still being written."""

    def __init__(self, path, linktype=1):
        self._f = open(path, "wb")
        self._f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, linktype))
        self._f.flush()

    def write(self, data):
        t = time.time()
        sec = int(t)
        self._f.write(struct.pack("<IIII", sec, int((t - sec) * 1e6), len(data), len(data)) + data)
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
            pass  # a dropped datagram must never kill a reader thread

    def close(self):
        self._sock.close()


def _is_ip_ethertype(et):
    return et in (b"\x08\x00", b"\x86\xdd")


class PacketOutput:
    """Shared sink for the readers. Thread-safety: a PacketOutput is only
    ever fed by one reader thread (the one matching cfg.source)."""

    def __init__(self, cfg: PacketOutputConfig):
        self.cfg = cfg
        self.n_packets = 0
        self.n_bytes = 0
        self.n_pcap_skipped_non_ip = 0
        self.udp = UdpSink(cfg.host, cfg.port) if cfg.udp else None
        self.pcap = PcapWriter(cfg.pcap_path, LINKTYPE[cfg.level]) if cfg.pcap else None

    def raw(self, data):
        """A GSE item or BBFRAME exactly as leandvb wrote it."""
        if self.cfg.level == LVL_RAW:
            self._emit(data, True)

    def frame(self, frame):
        """A reassembled GSE PDU as an Ethernet-like frame (14-byte header + PDU)."""
        if self.cfg.level == LVL_ETH:
            self._emit(frame, True)
        elif self.cfg.level == LVL_PDU:
            pdu = frame[14:]
            self._emit(pdu, _is_ip_ethertype(frame[12:14]))

    def _emit(self, data, pcap_ok):
        self.n_packets += 1
        self.n_bytes += len(data)
        if self.udp is not None:
            self.udp.send(data)
        if self.pcap is not None:
            if pcap_ok:
                self.pcap.write(data)
            else:
                self.n_pcap_skipped_non_ip += 1

    def summary(self):
        s = f"Packet output [{self.cfg.describe()}]: {self.n_packets} packets, {self.n_bytes} bytes"
        if self.n_pcap_skipped_non_ip:
            s += f"; {self.n_pcap_skipped_non_ip} non-IP PDUs sent to UDP but not written to the PCAP (RAW-IP link type)"
        return s

    def close(self):
        if self.udp is not None:
            self.udp.close()
        if self.pcap is not None:
            self.pcap.close()


class GseItemSplitter:
    """Cuts a concatenated GSE byte stream (--fd-gse) into whole GSE items.

    Zero padding (16 zero bits) can appear in the stream; once a padding
    run starts, every zero byte up to the next non-zero byte is skipped.
    Inferred, not verified on air: padding run length parity is unknowable
    from this stream alone (frame boundaries are not in it), and a
    continuation fragment (S=E=0) that starts right after padding would be
    mis-skipped. The BBFRAME source does not have this ambiguity.
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
                buf.clear()  # cannot come from leandvb (it CRC-checks first); resync by dropping
                return items
            end = p + BBH_LEN + dfl_bits // 8
            if end > n:
                break
            items.append(bytes(buf[i:end]))
            i = end
        del buf[:i]
        return items


def split_data_field(data):
    """Split one BBFRAME data field into GSE items. Because the field's end
    is known, zero padding is unambiguous: 16 zero bits mean "padding to the
    end of the field". Returns (items, overran) - overran is True if an item
    claims more bytes than the field has left."""
    items = []
    i, n = 0, len(data)
    while n - i >= 2:
        if data[i] == 0 and data[i + 1] == 0:
            break
        end = i + 2 + (((data[i] & 0x0F) << 8) | data[i + 1])
        if end > n:
            return items, True
        items.append(data[i:end])
        i = end
    return items, False


class BbframeReader(QThread):
    """Reads leandvb's --fd-bbf pipe. Raw level forwards each BBFRAME as-is;
    pdu / eth levels de-encapsulate generic-continuous (GSE) BBFRAMEs with the
    same decap code the GUI's flow analysis uses. Only unblocks (EOF) once
    leandvb exits - the chain's teardown kills leandvb before joining."""

    summary = Signal(str)

    def __init__(self, read_fd, output: PacketOutput, parent=None):
        super().__init__(parent)
        self.read_fd = read_fd
        self.output = output
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        cfg = self.output.cfg
        splitter = BbframeSplitter(cfg.l3sync_effective())
        prefix = 1 if cfg.l3sync_effective() else 0
        frag_table = {}
        stats = defaultdict(int)
        counts = Counter()
        try:
            while self.running:
                try:
                    chunk = os.read(self.read_fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                for bbf in splitter.feed(chunk):
                    counts["bbframes"] += 1
                    self.output.raw(bbf)
                    if cfg.level == LVL_RAW:
                        continue
                    body = bbf[prefix:]
                    st = body[0] >> 6
                    counts[STREAM_TYPE_NAMES[st]] += 1
                    if st != 1:
                        continue  # TS / packetized streams are not GSE
                    dfl = ((body[4] << 8) | body[5]) // 8
                    items, overran = split_data_field(body[BBH_LEN:BBH_LEN + dfl])
                    if overran:
                        counts["gse_item_ran_past_data_field"] += 1
                    for item in items:
                        out_frames = []
                        gd.decap_one_item(gd.BitReader(item), frag_table, out_frames, stats)
                        for f in out_frames:
                            self.output.frame(f)
                    gd.prune_stale_fragments(frag_table, 5.0)
        finally:
            try:
                os.close(self.read_fd)
            except OSError:
                pass
            self.summary.emit("BBFRAME reader: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
