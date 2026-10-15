"""Packet output selector (GSE chain): which leandvb stream to use, at what
encapsulation level, and where to send it (UDP and/or PCAP file)."""
import os
import time

from PySide6.QtWidgets import QCheckBox, QComboBox, QGridLayout, QGroupBox, QLabel, QLineEdit

from ..packet_out import LVL_ETH, LVL_PDU, LVL_RAW, SRC_BBF, SRC_GSE, PacketOutputConfig


class PacketOutputPanel(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Packet Output (GSE chain)", parent)
        g = QGridLayout(self)

        g.addWidget(QLabel("Source:"), 0, 0)
        self.source_combo = QComboBox()
        self.source_combo.addItem("GSE stream (--fd-gse)", SRC_GSE)
        self.source_combo.addItem("BBFRAMEs (--fd-bbf)", SRC_BBF)
        self.source_combo.setToolTip(
            "BBFRAMEs carry frame boundaries and the BBHEADER, so zero padding "
            "and stream type are unambiguous; the GSE stream does not.")
        g.addWidget(self.source_combo, 0, 1)

        g.addWidget(QLabel("Level:"), 0, 2)
        self.level_combo = QComboBox()
        self.level_combo.addItem("Raw (GSE item / BBFRAME)", LVL_RAW)
        self.level_combo.addItem("De-encapsulated (PDU only)", LVL_PDU)
        self.level_combo.addItem("Re-encapsulated (Ethernet frame)", LVL_ETH)
        self.level_combo.setCurrentIndex(2)
        self.level_combo.setToolTip(
            "PDU: the payload (e.g. IP packet) with GSE headers removed.\n"
            "Ethernet: PDU wrapped in a synthesized Ethernet header - opens "
            "directly in Wireshark.")
        g.addWidget(self.level_combo, 0, 3)

        self.l3sync_check = QCheckBox("Prefix 0xB8 (--bbf-l3sync)")
        g.addWidget(self.l3sync_check, 0, 4)

        self.udp_check = QCheckBox("UDP:")
        g.addWidget(self.udp_check, 1, 0)
        self.addr_edit = QLineEdit("127.0.0.1:5000")
        self.addr_edit.setFixedWidth(150)
        g.addWidget(self.addr_edit, 1, 1)

        self.pcap_check = QCheckBox("PCAP file:")
        g.addWidget(self.pcap_check, 1, 2)
        self.pcap_edit = QLineEdit("PCAPS/leangui_{ts}.pcap")
        self.pcap_edit.setToolTip("{ts} is replaced by the start time of each run, so runs never overwrite each other.")
        g.addWidget(self.pcap_edit, 1, 3, 1, 2)

        self.source_combo.currentIndexChanged.connect(self._sync_l3sync)
        self.level_combo.currentIndexChanged.connect(self._sync_l3sync)
        self._sync_l3sync()

    def _sync_l3sync(self):
        ok = self.source_combo.currentData() == SRC_BBF and self.level_combo.currentData() == LVL_RAW
        self.l3sync_check.setEnabled(ok)
        if not ok:
            self.l3sync_check.setChecked(False)

    def config(self) -> PacketOutputConfig:
        """Raises ValueError (with a user-readable message) on bad input."""
        cfg = PacketOutputConfig(
            source=self.source_combo.currentData(),
            level=self.level_combo.currentData(),
            udp=self.udp_check.isChecked(),
            pcap=self.pcap_check.isChecked(),
            bbf_l3sync=self.l3sync_check.isChecked(),
        )
        if cfg.udp:
            host, _, port_text = self.addr_edit.text().strip().rpartition(":")
            try:
                port = int(port_text)
            except ValueError:
                port = 0
            if not host or not (0 < port < 65536):
                raise ValueError("UDP needs host:port, e.g. 127.0.0.1:5000")
            cfg.host, cfg.port = host, port
        if cfg.pcap:
            path = self.pcap_edit.text().strip()
            if not path:
                raise ValueError("PCAP needs a file path")
            cfg.pcap_path = os.path.expanduser(path).replace("{ts}", time.strftime("%Y%m%d_%H%M%S"))
        return cfg
