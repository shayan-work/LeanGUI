"""Low-level GSE bit-packing/CRC helpers and the headerless-stream item
parser, ported from gse_decap.py and gse_decap_headerless.py.

leandvb's s2_deframer (leansdr/dvbs2.h, s2_deframer::handle_bbframe) does
exactly `write(fd_gse, data, dfl/8)` for a generic-stream BBFRAME: the
10-byte BBHEADER is already stripped and validated by leandvb itself, and
only the DFL data field is written - so a --fd-gse dump is a continuous,
gap-free stream of concatenated GSE items with no frame stride to scan for
(unlike gse_decap.py's offline --align mode, which is for a raw BBFRAME
dump that still has BBHEADERs in it). decap_one_item() below is the
streaming counterpart: it consumes exactly one GSE item from the front of
a BitReader and leaves the reader positioned right after it, so a caller
can keep appending freshly-arrived bytes and re-invoke it as more of the
stream shows up.
"""
import time

ETHER_ADDR_LEN = 6


# --------------------------------------------------------------------------
# CRC-32 (GSE fragmentation) - non-reflected, poly 0x04C11DB7, init
# 0xFFFFFFFF, no final xor. Matches crc32_init()/crc32_calc() in gr-dvbgse's
# bbheader_sink_impl.cc.
# --------------------------------------------------------------------------
def _build_crc32_table():
    table = [0] * 256
    for i in range(256):
        k = 0
        j = (i << 24) | 0x800000
        while j != 0x80000000:
            if (k ^ j) & 0x80000000:
                k = ((k << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                k = (k << 1) & 0xFFFFFFFF
            j = (j << 1) & 0xFFFFFFFF
        table[i] = k
    return table


CRC32_TABLE = _build_crc32_table()


def crc32_calc(data, crc):
    for byte in data:
        crc = ((crc << 8) ^ CRC32_TABLE[((crc >> 24) ^ byte) & 0xFF]) & 0xFFFFFFFF
    return crc


# --------------------------------------------------------------------------
# MSB-first bit reader over a packed byte buffer (matches the bit order
# gr-dvbgse uses throughout: "MSB is sent first").
# --------------------------------------------------------------------------
class BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.nbits = len(data) * 8
        self.pos = 0  # bit position

    def remaining(self):
        return self.nbits - self.pos

    def read_bit(self):
        byte = self.data[self.pos >> 3]
        shift = 7 - (self.pos & 7)
        self.pos += 1
        return (byte >> shift) & 1

    def read_bits(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | self.read_bit()
        return v


class ReassemblyBuf:
    __slots__ = ("buf", "crc", "last_update")

    def __init__(self):
        self.buf = bytearray()
        self.crc = 0
        self.last_update = time.monotonic()


def prune_stale_fragments(frag_table, max_age_seconds, now=None):
    """Evict frag_table entries that haven't seen a chunk in max_age_seconds.

    A fragmented PDU abandoned mid-stream (lost lock, dropped/corrupted
    BBFRAMEs, a link fade) never reaches the start==0,end==1 branch below
    that normally deletes its entry - left alone, its ReassemblyBuf.buf
    would sit there consuming memory for the rest of the session. This is
    a plain time-based sweep rather than something decap_one_item() does
    itself, since decap_one_item() is a pure per-item parser reused by
    offline tooling too, which has no natural "tick" to run this on and may
    not want wall-clock-based eviction at all.

    Returns the number of entries pruned.
    """
    if now is None:
        now = time.monotonic()
    stale = [frag_id for frag_id, rb in frag_table.items()
             if now - rb.last_update >= max_age_seconds]
    for frag_id in stale:
        del frag_table[frag_id]
    return len(stale)


def decap_one_item(bits_reader, frag_table, out_frames, stats):
    """Parse exactly one GSE packet from the front of bits_reader.

    Every GSE header field here is a multiple of 8 bits (start/end/lt/
    gse_length is 16 bits fixed; frag_id, total_length, protocol_type,
    label and the payload itself are all whole bytes too), so bits_reader
    always ends up byte-aligned again after a successful parse - callers
    can safely drop the consumed bytes from the front of their buffer
    (bits_reader.pos // 8) and keep the rest for next time.

    Returns False if there isn't enough data left in bits_reader to safely
    parse a full item - the caller should stop and wait for more bytes to
    arrive rather than treating this as a hard end of stream.
    """
    if bits_reader.remaining() < 16:
        return False

    start_indicator = bits_reader.read_bit()
    end_indicator = bits_reader.read_bit()
    lt = (bits_reader.read_bit() << 1) | bits_reader.read_bit()
    gse_length = bits_reader.read_bits(12)
    if start_indicator == 0 and end_indicator == 0 and lt == 0 and gse_length == 0:
        # Real GSE padding is a full 16 zero bits (S=0, E=0, LT=0, and
        # GSE_Length=0 too) - not present in this concatenated stream, so
        # treat as end of usable data (e.g. trailing zero-fill at EOF).
        # Checking gse_length here matters: a genuine continuation/end
        # fragment (S=0, E=0) always has LT forced to 0 by the spec too
        # (LT only means something when S=1), but it carries a non-zero
        # GSE_Length (frag_id + real payload bytes) - without this check,
        # every such fragment after the first one in a PDU was misread as
        # padding and reassembly silently stalled forever.
        return False

    frag_id = 0
    if start_indicator == 0 or end_indicator == 0:
        if bits_reader.remaining() < 8:
            return False
        frag_id = bits_reader.read_bits(8)
        gse_length -= 1

    crc_partial = 0
    total_length_bytes = None
    if start_indicator == 1 and end_indicator == 0:
        if bits_reader.remaining() < 16:
            return False
        total_length_bytes = bytes(bits_reader.read_bits(8) for _ in range(2))
        crc_partial = crc32_calc(total_length_bytes, 0xFFFFFFFF)
        gse_length -= 2
        frag_table[frag_id] = ReassemblyBuf()

    protocol_type = None
    label = None
    if start_indicator == 1:
        if bits_reader.remaining() < 16:
            return False
        protocol_type = bytes(bits_reader.read_bits(8) for _ in range(2))
        if total_length_bytes is not None:
            crc_partial = crc32_calc(protocol_type, crc_partial)
        gse_length -= 2
        if lt == 0:
            if bits_reader.remaining() < 48:
                return False
            label = bytes(bits_reader.read_bits(8) for _ in range(ETHER_ADDR_LEN))
            if total_length_bytes is not None:
                crc_partial = crc32_calc(label, crc_partial)
            gse_length -= 6
        elif lt == 1:
            if bits_reader.remaining() < 24:
                return False
            label = bytes(bits_reader.read_bits(8) for _ in range(3))
            if total_length_bytes is not None:
                crc_partial = crc32_calc(label, crc_partial)
            gse_length -= 3

    if gse_length < 0:
        return False

    if start_indicator == 1 and end_indicator == 1:
        if bits_reader.remaining() < gse_length * 8:
            return False
        payload = bytes(bits_reader.read_bits(8) for _ in range(gse_length))
        frame = bytearray()
        frame += (label if label else b"\x00" * ETHER_ADDR_LEN)
        frame += b"\x00" * ETHER_ADDR_LEN
        frame += (protocol_type if protocol_type else b"\x00\x00")
        frame += payload
        out_frames.append(bytes(frame))
        stats["frames_ok"] += 1

    elif start_indicator == 1 and end_indicator == 0:
        if bits_reader.remaining() < gse_length * 8:
            return False
        rb = frag_table.get(frag_id)
        if rb is None:
            stats["frag_no_buffer"] += 1
            bits_reader.read_bits(gse_length * 8)
            return True
        rb.buf += (label if label else b"\x00" * ETHER_ADDR_LEN)
        rb.buf += b"\x00" * ETHER_ADDR_LEN
        rb.buf += (protocol_type if protocol_type else b"\x00\x00")
        chunk = bytes(bits_reader.read_bits(8) for _ in range(gse_length))
        rb.buf += chunk
        rb.crc = crc32_calc(chunk, crc_partial)
        rb.last_update = time.monotonic()

    elif start_indicator == 0 and end_indicator == 0:
        if bits_reader.remaining() < gse_length * 8:
            return False
        rb = frag_table.get(frag_id)
        if rb is None:
            stats["frag_no_buffer"] += 1
            bits_reader.read_bits(gse_length * 8)
            return True
        chunk = bytes(bits_reader.read_bits(8) for _ in range(gse_length))
        rb.buf += chunk
        rb.crc = crc32_calc(chunk, rb.crc)
        rb.last_update = time.monotonic()

    elif start_indicator == 0 and end_indicator == 1:
        payload_len = gse_length - 4
        if payload_len < 0 or bits_reader.remaining() < gse_length * 8:
            return False
        rb = frag_table.get(frag_id)
        if rb is None:
            stats["frag_no_buffer"] += 1
            bits_reader.read_bits(gse_length * 8)
            return True
        chunk = bytes(bits_reader.read_bits(8) for _ in range(payload_len))
        rb.buf += chunk
        rb.crc = crc32_calc(chunk, rb.crc)
        crc_trailer = bytes(bits_reader.read_bits(8) for _ in range(4))
        rb.crc = crc32_calc(crc_trailer, rb.crc)
        if rb.crc == 0:
            out_frames.append(bytes(rb.buf))
            stats["frames_ok"] += 1
        else:
            stats["frag_crc_fail"] += 1
        del frag_table[frag_id]
    return True
