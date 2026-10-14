"""UDP output selector: stream GSE packets or BBFRAMEs to host:port."""
from PySide6.QtWidgets import QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit

from ..udp_out import MODE_BBF, MODE_GSE, MODE_OFF, UdpOutputConfig


class UdpOutputPanel(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("UDP Output (GSE chain)", parent)
        layout = QHBoxLayout(self)
        layout.addWidget(QLabel("Stream:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Off", MODE_OFF)
        self.mode_combo.addItem("GSE packets", MODE_GSE)
        self.mode_combo.addItem("BBFRAMEs", MODE_BBF)
        layout.addWidget(self.mode_combo)
        layout.addWidget(QLabel("Host:Port:"))
        self.addr_edit = QLineEdit("127.0.0.1:5000")
        self.addr_edit.setFixedWidth(150)
        layout.addWidget(self.addr_edit)
        self.l3sync_check = QCheckBox("Prefix 0xB8 (--bbf-l3sync)")
        layout.addWidget(self.l3sync_check)
        layout.addStretch()

    def mode(self):
        return self.mode_combo.currentData()

    def config(self) -> UdpOutputConfig:
        """Raises ValueError on a malformed host:port."""
        host, _, port_text = self.addr_edit.text().strip().rpartition(":")
        port = int(port_text)
        if not host or not (0 < port < 65536):
            raise ValueError("expected host:port")
        return UdpOutputConfig(mode=self.mode(), host=host, port=port,
                               bbf_l3sync=self.l3sync_check.isChecked())
