"""Spectrum plot: PSD curve, SNR readout, and the draggable tuning region."""
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Signal


class SpectrumPlotWidget(pg.PlotWidget):
    region_changed = Signal()
    region_change_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBackground('k')
        self.showGrid(x=True, y=True, alpha=0.3)
        self.setLabel('left', 'Relative Power', units='dB')
        self.setLabel('bottom', 'Frequency', units='Hz')
        self.setTitle('Signal Spectrum')
        # Placeholder until real data arrives and auto_scale_y() takes over -
        # a fixed range doesn't generalize across recordings at different
        # absolute levels (a real capture's noise floor can easily sit well
        # below -80 dB, clipping it out of view entirely).
        self.setYRange(-80, 20)
        self.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        # The view itself isn't something the user drags/zooms by hand -
        # it's driven entirely by the signal parameters (center_view(),
        # called whenever the offset/sample-rate fields change). Disabling
        # mouse pan/zoom here doesn't affect the tuning_region below,
        # which has its own independent drag handling.
        self.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        self.setMouseEnabled(x=False, y=False)
        self.curve = self.plot(pen=pg.mkPen('c', width=1.5))
        # anchor=(0, 0) pins the text's own top-left corner to pos, so it
        # extends downward/rightward from there and stays inside the view.
        # anchor=(0, 1) (bottom-left) made it extend upward instead, which
        # ran it straight off the top edge and got clipped by the ViewBox.
        self.snr_text = pg.TextItem(text="SNR: -- dB", color='w', anchor=(0, 0))
        self.addItem(self.snr_text, ignoreBounds=True)

        # Shaded region representing occupied bandwidth (BW = Rs * (1 + alpha))
        self.tuning_region = pg.LinearRegionItem(
            values=[-625000, 625000],
            orientation='vertical',
            brush=pg.mkBrush(0, 100, 255, 50),
            pen=pg.mkPen('r', style=pg.QtCore.Qt.DashLine),
        )
        self.addItem(self.tuning_region)
        self.tuning_region.setMovable(True)

        # Dashed white line indicating exact center frequency (offset)
        self.center_line = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen('w', style=pg.QtCore.Qt.DashLine))
        self.addItem(self.center_line)

        self.tuning_region.sigRegionChanged.connect(self.region_changed)
        self.tuning_region.sigRegionChangeFinished.connect(self.region_change_finished)

    def update_curve(self, freqs, psd_db):
        self.curve.setData(freqs, psd_db)
        self._auto_scale_y(psd_db)

    def _auto_scale_y(self, psd_db):
        """Fit the Y range to the actual signal, not a fixed guess.

        psd_db is already the (time-averaged) trace, so it doesn't jitter
        frame to frame - the recording's absolute level is what varies, and
        a fixed range like -80..20 dB only suits recordings around one
        particular level. Below that, the true noise floor gets clipped
        off the bottom of the view entirely.
        """
        if psd_db is None or len(psd_db) == 0:
            return
        lo = float(np.min(psd_db))
        hi = float(np.max(psd_db))
        if hi - lo < 10:  # guard against a degenerate near-flat trace
            mid = (hi + lo) / 2.0
            lo, hi = mid - 5.0, mid + 5.0
        self.setYRange(lo - 5.0, hi + 10.0, padding=0)

    def set_region(self, f_min, f_max):
        self.tuning_region.setRegion([f_min, f_max])

    def get_region(self):
        return self.tuning_region.getRegion()

    def edge_dragging(self):
        line0, line1 = self.tuning_region.lines
        return getattr(line0, 'moving', False) or getattr(line1, 'moving', False)

    def set_center(self, fc_hz):
        self.center_line.setValue(fc_hz)

    def center_view(self, fc_hz, span_hz):
        """Auto-position the view on fc_hz; not a user pan/zoom action."""
        self.setXRange(fc_hz - span_hz / 2.0, fc_hz + span_hz / 2.0, padding=0)

    def set_snr_text(self, text):
        self.snr_text.setText(text)

    def position_snr_text(self):
        # Pin to the top-left corner regardless of zoom/pan
        vb = self.getViewBox()
        (xmin, xmax), (ymin, ymax) = vb.viewRange()
        margin_x = 0.02 * (xmax - xmin)
        margin_y = 0.05 * (ymax - ymin)
        self.snr_text.setPos(xmin + margin_x, ymax - margin_y)
