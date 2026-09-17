"""Video output panel; a decode chain embeds its player (e.g. mpv) via
mpv_container's X11 window id - NOT this widget's own winId(). mpv's --wid
reparents its own top-level window as a foreign child filling whatever
window id it's given; handing it this panel's own winId() would make that
foreign window a *sibling* of any Qt child widgets added to the same
panel (like the "encrypted" label below), fighting them for the same
screen area outside Qt's own paint/stacking control - hiding/killing mpv
doesn't reliably reveal a sibling Qt widget underneath in that setup. A
dedicated child widget purely for mpv to embed into, separate from
anything else this panel ever shows, avoids that: hiding mpv_container
unmaps the whole X subtree mpv reparented itself into, cleanly, and the
label (or the Wayland placeholder) is a plain sibling widget with no
foreign window ever sharing its space.

Under a pure Wayland session there's no reliable way to embed an external
process's window this way (--wid only works via XWayland compatibility, and
not always), so the chain skips --wid entirely there (see
LeanDVBChain._spawn_mpv) and mpv opens its own top-level window instead.
Show a placeholder in that case so the user isn't left staring at a dead
black box wondering where the video went.

set_encrypted() covers a different dead-black-box case: content that
demods/decodes just fine but is CA-scrambled at the source (see
io_threads.TSRecorder._scan_scrambling and
LeanDVBChain._on_encryption_status) - no player can show a picture for
that regardless of lock quality, so the chain skips spawning one at all
once it's detected, and this overlay explains why instead of just leaving
the panel black with no indication anything was even found.
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QStackedLayout, QWidget


class VideoPanelWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: black;")
        self.setMinimumHeight(280)

        # QStackedLayout (not QVBoxLayout): mpv_container and the overlay
        # labels occupy the exact same area, toggled by which is raised/
        # visible, rather than being stacked vertically.
        layout = QStackedLayout(self)
        layout.setStackingMode(QStackedLayout.StackingMode.StackAll)

        # The one and only widget a chain should ever pass to mpv's --wid -
        # see the module docstring for why not self.winId() directly.
        self.mpv_container = QWidget()
        self.mpv_container.setStyleSheet("background-color: black;")
        layout.addWidget(self.mpv_container)

        self._overlay_label = None
        if os.environ.get('XDG_SESSION_TYPE') == 'wayland':
            wayland_label = QLabel("Video playing in a separate window (Wayland session detected)")
            wayland_label.setStyleSheet("color: white; background-color: black;")
            wayland_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            wayland_label.setWordWrap(True)
            layout.addWidget(wayland_label)
            self._overlay_label = wayland_label

        # Hidden until set_encrypted(True). Stacked above mpv_container in
        # the same QStackedLayout, so showing it always wins visually
        # regardless of mpv_container's own state - no killing/hiding
        # race with the mpv subprocess required.
        self._encrypted_label = QLabel(
            "\U0001f512 Content is encrypted (conditional access)\n"
            "leandvb decoded the signal, but this channel cannot be displayed\n"
            "without descrambling keys."
        )
        self._encrypted_label.setStyleSheet(
            "color: #ff6b6b; font-weight: bold; font-size: 13px; background-color: black;"
        )
        self._encrypted_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._encrypted_label.setWordWrap(True)
        self._encrypted_label.hide()
        layout.addWidget(self._encrypted_label)

    def set_encrypted(self, encrypted: bool):
        """Show/hide the encrypted-content overlay.

        mpv_container must be *hidden* (unmapped), not just painted over,
        for the label to actually become visible: mpv_container is a
        native widget (forced by handing its winId() to mpv - see the
        module docstring), and mpv reparents its own top-level window as
        a *child* of it. In X11, a child window always paints in front of
        its parent's own drawing surface, unconditionally - raising an
        alien (non-native) sibling widget like _encrypted_label via Qt's
        widget stacking has no effect on that, because Qt's raise()/
        lower() only reorders real windows, and _encrypted_label doesn't
        have one of its own to reorder. Unmapping mpv_container removes
        the whole native subtree (mpv's window included) from the screen
        entirely, which is what actually lets the label - painted
        directly onto this panel's own surface - show through.
        """
        self.mpv_container.setVisible(not encrypted)
        self._encrypted_label.setVisible(encrypted)
