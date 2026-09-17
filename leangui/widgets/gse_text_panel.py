"""Left-of-video panel showing GSE/IP flow status and the content of any
flow identified as plain text. Populated by LeanDVBGSEChain; stays hidden
until that chain actually reports a text flow (most GSE traffic is video/
binary, which goes to the video panel / is left alone instead)."""
from PySide6.QtWidgets import QGroupBox, QLabel, QPlainTextEdit, QVBoxLayout


class GseTextPanel(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("GSE / IP Packet Data", parent)
        layout = QVBoxLayout(self)

        self.flow_summary = QLabel("No flows yet.")
        self.flow_summary.setWordWrap(True)
        layout.addWidget(self.flow_summary)

        self.text_view = QPlainTextEdit()
        self.text_view.setReadOnly(True)
        self.text_view.setMaximumBlockCount(5000)  # cap memory on a long-running capture
        layout.addWidget(self.text_view)

        self._flow_lines = {}  # flow_key -> latest status line

    def update_flow_summary(self, flow_key, status_line):
        self._flow_lines[flow_key] = status_line
        self.flow_summary.setText("\n".join(self._flow_lines.values()))

    def append_text(self, flow_key, text):
        self.text_view.appendPlainText(f"--- {flow_key} ---\n{text}")

    def clear(self):
        self._flow_lines.clear()
        self.flow_summary.setText("No flows yet.")
        self.text_view.clear()
