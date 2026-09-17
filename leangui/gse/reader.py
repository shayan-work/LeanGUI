"""Background thread: streams leandvb's raw --fd-gse dump, reassembles GSE
PDUs into flows live, and emits Qt signals for the GUI as each flow's
content is identified - instead of the offline gse_receive.py's model of
"read the whole file, then print a manifest at the end".
"""
import os
import time
from collections import defaultdict

from PySide6.QtCore import QThread, Signal

from . import decap as gd
from .flows import classify_frame, flow_key_str, sniff_content, strip_rtp_if_present

READ_CHUNK = 65536

#: Bytes to accumulate per flow before running sniff_content() on it. Small
#: enough that short-lived/text flows get identified quickly, big enough to
#: give looks_like_ts()/the magic-number table a fair sample to work with.
#: A flow that never reaches this (e.g. a single short status/telemetry
#: datagram - very much including "text data" one-liners, the main reason
#: this whole panel exists) still gets sniffed by the idle-flush below.
SNIFF_AFTER_BYTES = 2048

#: How long a not-yet-sniffed flow can go without a new packet before it
#: gets sniffed anyway with whatever it has. Checked opportunistically
#: whenever any frame arrives, so it's not exact, but it's not meant to be -
#: it just needs to keep a lone small packet from sitting unclassified
#: forever while the flow it belongs to has gone quiet.
IDLE_FLUSH_SECONDS = 0.75

#: How long an in-progress fragment reassembly (frag_table entry) can go
#: without a new chunk before it's assumed abandoned (lost lock, dropped/
#: corrupted BBFRAMEs, a link fade) and pruned. Long enough to survive the
#: normal delay between fragments of one PDU being reassembled, short
#: enough to bound memory during a real fade instead of leaking a growing
#: ReassemblyBuf.buf for the rest of the session. Checked at the same
#: cadence as the idle-flow flush above, for the same reason: cheap to
#: check opportunistically whenever a chunk of data is processed, rather
#: than on every single item.
FRAG_TTL_SECONDS = 5.0


class GSEReader(QThread):
    #: (flow_key, one-line human-readable status) - updated as packets for
    #: a flow keep arriving, not just once at the end.
    flow_update = Signal(str, str)
    #: (flow_key, decoded text) for a flow identified as text/plain.
    text_chunk = Signal(str, str)
    #: raw MPEG-TS bytes for a flow identified as video/MPEG-TS (RTP header
    #: already stripped if it was RTP-wrapped) - feed straight to a player.
    video_chunk = Signal(bytes)
    #: free-form status lines for flows that aren't text/video (same spirit
    #: as DecodeChain.debug_line).
    debug_line = Signal(str)

    def __init__(self, read_fd, parent=None):
        super().__init__(parent)
        self.read_fd = read_fd
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        buf = bytearray()
        frag_table = {}
        stats = defaultdict(int)
        flows = {}  # flow_key -> {"count", "bytes", "sniffed", "kind", "chunks"}

        try:
            while self.running:
                try:
                    chunk = os.read(self.read_fd, READ_CHUNK)
                except OSError:
                    break
                if not chunk:
                    break  # leandvb exited / pipe closed
                buf += chunk

                out_frames = []
                while True:
                    br = gd.BitReader(bytes(buf))
                    ok = gd.decap_one_item(br, frag_table, out_frames, stats)
                    if not ok:
                        break
                    del buf[:br.pos // 8]

                for frame in out_frames:
                    self._handle_frame(frame, flows)
                if out_frames:
                    self._flush_idle_flows(flows)
                    pruned = gd.prune_stale_fragments(frag_table, FRAG_TTL_SECONDS)
                    if pruned:
                        self.debug_line.emit(
                            f"[gse] pruned {pruned} stale fragment(s) (lock loss or dropped frames)")
        finally:
            # End of stream (leandvb exited / we were stopped): anything
            # still waiting on more data to reach SNIFF_AFTER_BYTES never
            # will now, so sniff it with whatever it has rather than
            # silently dropping it.
            for flow_key, fl in flows.items():
                if not fl["sniffed"] and fl["bytes"] > 0:
                    self._sniff_flow(flow_key, fl)
            try:
                os.close(self.read_fd)
            except OSError:
                pass

    def _handle_frame(self, frame, flows):
        result = classify_frame(frame)
        if result is None:
            return
        key, proto_label, payload = result
        if payload is None:
            return  # ARP / unhandled ethertype - nothing to carry forward

        flow_key = flow_key_str(key)
        fl = flows.get(flow_key)
        if fl is None:
            fl = {"proto": proto_label, "chunks": [], "count": 0, "bytes": 0,
                  "sniffed": False, "kind": None, "last_seen": 0.0}
            flows[flow_key] = fl

        fl["count"] += 1
        fl["bytes"] += len(payload)
        fl["last_seen"] = time.monotonic()

        if not fl["sniffed"]:
            fl["chunks"].append(payload)
            if fl["bytes"] >= SNIFF_AFTER_BYTES:
                self._sniff_flow(flow_key, fl)
        else:
            self._emit_live_payload(flow_key, fl["kind"], payload)

        self.flow_update.emit(
            flow_key,
            f"{flow_key}  [{fl['proto']}]  pkts={fl['count']}  bytes={fl['bytes']}  "
            f"type={fl['kind'] or 'detecting...'}",
        )

    def _flush_idle_flows(self, flows):
        now = time.monotonic()
        for flow_key, fl in flows.items():
            if not fl["sniffed"] and now - fl["last_seen"] >= IDLE_FLUSH_SECONDS:
                self._sniff_flow(flow_key, fl)

    def _sniff_flow(self, flow_key, fl):
        desc, ext, data = sniff_content(fl["chunks"])
        fl["sniffed"] = True
        fl["kind"] = ext
        fl["chunks"] = []  # only needed to reach a sniffing decision; drop it
        if ext == "ts":
            self.video_chunk.emit(data)
        elif ext == "txt":
            self.text_chunk.emit(flow_key, data.decode("utf-8", errors="replace"))
        else:
            self.debug_line.emit(f"[gse] flow {flow_key}: {desc} (not streamed live)")

    def _emit_live_payload(self, flow_key, kind, payload):
        if kind == "ts":
            # Re-check per packet rather than trusting a header_len cached
            # from the first sniff - cheap, and stays correct if RTP
            # framing details vary slightly packet to packet.
            stripped, _ = strip_rtp_if_present([payload])
            for chunk in stripped:
                self.video_chunk.emit(chunk)
        elif kind == "txt":
            self.text_chunk.emit(flow_key, payload.decode("utf-8", errors="replace"))
