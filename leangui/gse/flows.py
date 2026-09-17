"""IP-flow classification and content-type sniffing for reassembled GSE
PDUs, ported from gse_receive.py.

classify_frame() turns one reassembled Ethernet-ish frame (as produced by
decap.decap_one_item) into (flow_key, proto_label, payload); sniff_content()
looks at a flow's accumulated payload chunks and guesses what's actually
inside (MPEG-TS/video, text, a handful of common file types, or unknown
binary) so the GUI can decide where to route it.
"""
import struct

IP_PROTO_NAMES = {1: "ICMP", 6: "TCP", 17: "UDP", 58: "ICMPv6"}


def parse_ipv4(pkt):
    if len(pkt) < 20:
        return None
    ver_ihl = pkt[0]
    if ver_ihl >> 4 != 4:
        return None
    ihl = (ver_ihl & 0x0F) * 4
    if len(pkt) < ihl:
        return None
    proto = pkt[9]
    src = ".".join(str(b) for b in pkt[12:16])
    dst = ".".join(str(b) for b in pkt[16:20])
    l4 = pkt[ihl:]
    return proto, src, dst, l4


def parse_ipv6(pkt):
    if len(pkt) < 40:
        return None
    if pkt[0] >> 4 != 6:
        return None
    proto = pkt[6]
    src = ":".join(f"{pkt[8+i]:02x}{pkt[9+i]:02x}" for i in range(0, 16, 2))
    dst = ":".join(f"{pkt[24+i]:02x}{pkt[25+i]:02x}" for i in range(0, 16, 2))
    l4 = pkt[40:]
    # Not walking IPv6 extension headers (hop-by-hop, routing, fragment...) -
    # good enough for the common case of a directly-encapsulated UDP/TCP/ICMPv6
    # payload, which is what a GSE/DVB-S2 IP link normally carries.
    return proto, src, dst, l4


def parse_udp(l4):
    if len(l4) < 8:
        return None
    sport, dport, length = struct.unpack(">HHH", l4[0:6])
    return sport, dport, l4[8:]


def parse_tcp(l4):
    if len(l4) < 20:
        return None
    sport, dport = struct.unpack(">HH", l4[0:4])
    doff = (l4[12] >> 4) * 4
    if doff < 20 or len(l4) < doff:
        doff = 20
    return sport, dport, l4[doff:]


def classify_frame(frame):
    """frame = 6(dst label)+6(src placeholder)+2(ethertype)+L3 packet.
    Returns (flow_key, proto_label, payload_bytes) or None if not
    something we can/should carry forward as flow data (e.g. ARP)."""
    if len(frame) < 14:
        return None
    ethertype = struct.unpack(">H", frame[12:14])[0]
    l3 = frame[14:]

    if ethertype == 0x0800:
        parsed = parse_ipv4(l3)
        if parsed is None:
            return None
        proto, src, dst, l4 = parsed
    elif ethertype == 0x86DD:
        parsed = parse_ipv6(l3)
        if parsed is None:
            return None
        proto, src, dst, l4 = parsed
    elif ethertype == 0x0806:
        return ("ARP", "ARP", None), "ARP", None
    else:
        return (f"ethertype_0x{ethertype:04x}", "OTHER", None), "OTHER", None

    proto_name = IP_PROTO_NAMES.get(proto, f"proto_{proto}")

    if proto == 17:  # UDP
        u = parse_udp(l4)
        if u is None:
            return None
        sport, dport, payload = u
        key = ("UDP", src, sport, dst, dport)
        return key, "UDP", payload
    elif proto == 6:  # TCP
        t = parse_tcp(l4)
        if t is None:
            return None
        sport, dport, payload = t
        key = ("TCP", src, sport, dst, dport)
        return key, "TCP", payload
    else:
        # ICMP/ICMPv6/other IP protocols: keep as one flow per (proto,src,dst),
        # payload = everything after the IP header.
        key = (proto_name, src, dst)
        return key, proto_name, l4


# --------------------------------------------------------------------------
# Content sniffing - decide what a flow's payload actually is.
# --------------------------------------------------------------------------

MAGIC_TABLE = [
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"%PDF", "application/pdf", "pdf"),
    (b"PK\x03\x04", "application/zip", "zip"),
    (b"PK\x05\x06", "application/zip", "zip"),
    (b"\x1f\x8b", "application/gzip", "gz"),
    (b"\x7fELF", "application/x-elf", "elf"),
    (b"ID3", "audio/mpeg", "mp3"),
    (b"OggS", "application/ogg", "ogg"),
    (b"fLaC", "audio/flac", "flac"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
]


def looks_like_ts(data, min_syncs=4):
    """Check for MPEG-TS 0x47 sync bytes recurring every 188 bytes."""
    if len(data) < 188 * min_syncs:
        min_syncs = max(1, len(data) // 188)
        if min_syncs == 0:
            return False
    if data[0] != 0x47:
        return False
    hits = sum(1 for i in range(min_syncs) if i * 188 < len(data) and data[i * 188] == 0x47)
    return hits == min_syncs and min_syncs > 0


def strip_rtp_if_present(chunks):
    """chunks: list of per-datagram UDP payloads for one flow.
    If they look like RTP (version==2) carrying MPEG-TS (payload type 33,
    or the de-RTP'd bytes align to 0x47), strip the RTP header from each
    datagram and return the de-RTP'd chunks + True. Otherwise return the
    original chunks + False."""
    if not chunks or len(chunks[0]) < 12:
        return chunks, False
    first = chunks[0]
    version = first[0] >> 6
    if version != 2:
        return chunks, False
    pt = first[1] & 0x7F
    cc = first[0] & 0x0F
    x = (first[0] >> 4) & 0x1
    header_len = 12 + cc * 4
    if x:
        if len(first) < header_len + 4:
            return chunks, False
        ext_len_words = struct.unpack(">H", first[header_len + 2:header_len + 4])[0]
        header_len += 4 + ext_len_words * 4
    if len(first) <= header_len:
        return chunks, False
    candidate_payload = first[header_len:]
    is_ts_pt = (pt == 33)
    is_ts_shape = candidate_payload[:1] == b"\x47"
    if not (is_ts_pt or is_ts_shape):
        return chunks, False
    stripped = []
    for c in chunks:
        if len(c) > header_len:
            stripped.append(c[header_len:])
    return stripped, True


def sniff_content(chunks):
    """chunks: ordered list of payload byte-strings for one flow.
    Returns (description, extension, output_bytes)."""
    stripped, was_rtp = strip_rtp_if_present(chunks)
    data = b"".join(stripped)

    if looks_like_ts(data):
        label = "video/MPEG-TS" + (" (was RTP-wrapped, header stripped)" if was_rtp else "")
        return label, "ts", data

    # magic numbers checked on the (possibly non-RTP) original data too,
    # in case RTP-stripping guessed wrong for a non-TS RTP payload.
    data_orig = b"".join(chunks)
    for magic, mime, ext in MAGIC_TABLE:
        if data_orig.startswith(magic):
            return f"{mime} (magic match)", ext, data_orig
    if len(data_orig) >= 8 and data_orig[4:8] == b"ftyp":
        return "video/mp4 (ISOBMFF)", "mp4", data_orig
    if len(data_orig) >= 12 and data_orig[0:4] == b"RIFF" and data_orig[8:12] == b"WAVE":
        return "audio/wav", "wav", data_orig

    sample = data_orig[:4096]
    if sample:
        printable = sum(1 for b in sample if 32 <= b <= 126 or b in (9, 10, 13))
        if printable / len(sample) > 0.95:
            try:
                sample.decode("utf-8")
                return "text/plain", "txt", data_orig
            except UnicodeDecodeError:
                pass

    return "application/octet-stream (unidentified binary)", "bin", data_orig


def flow_key_str(key):
    """Human-readable label for a classify_frame() flow key."""
    if key[0] in ("UDP", "TCP"):
        _, src, sport, dst, dport = key
        return f"{key[0]} {src}:{sport} -> {dst}:{dport}"
    proto_name, src, dst = key
    return f"{proto_name} {src} -> {dst}"
