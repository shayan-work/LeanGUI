"""Pure DSP helpers for the raw-file spectrum preview.

These operate on plain numpy arrays and have no Qt/GUI or decode-chain
dependency, so they're reusable and independently testable regardless of
which decode chain (if any) is running.
"""
import numpy as np


class EmptyFileError(Exception):
    """Raised by read_iq_chunk when the file has no samples left at all."""


class InsufficientSamplesError(Exception):
    """Raised by estimate_symbol_rate_hz when the file is too short to analyze."""
    
class IQRingBuffer:
    """Fixed-size circular buffer of complex64 IQ samples. Used to give
    estimate_symbol_rate_hz_from_iq() a sliding window of recent real-time
    samples without reallocating/copying on every incoming chunk."""

    def __init__(self, capacity):
        self.capacity = capacity
        self._buf = np.zeros(capacity, dtype=np.complex64)
        self._write_pos = 0
        self._filled = 0

    def write(self, chunk):
        n = len(chunk)
        if n >= self.capacity:
            self._buf[:] = chunk[-self.capacity:]
            self._write_pos = 0
            self._filled = self.capacity
            return
        end = self._write_pos + n
        if end <= self.capacity:
            self._buf[self._write_pos:end] = chunk
        else:
            first = self.capacity - self._write_pos
            self._buf[self._write_pos:] = chunk[:first]
            self._buf[:end - self.capacity] = chunk[first:]
        self._write_pos = end % self.capacity
        self._filled = min(self.capacity, self._filled + n)

    def contents(self):
        """Buffered samples in chronological order."""
        if self._filled < self.capacity:
            return self._buf[:self._filled].copy()
        return np.concatenate((self._buf[self._write_pos:], self._buf[:self._write_pos]))


def read_iq_chunk(file_handle, fft_size):
    """Read one fft_size-sample complex64 chunk from an open binary file.

    Loops back to the start of the file on EOF. Returns None if a chunk was
    read but still came up short after the loop-back retry (caller should
    just skip this tick and try again next time). Raises EmptyFileError if
    the file has no data at all.
    """
    bytes_to_read = fft_size * 2 * 4  # I+Q, 4 bytes per float32 each
    chunk_bytes = file_handle.read(bytes_to_read)

    if len(chunk_bytes) < bytes_to_read:
        file_handle.seek(0)
        chunk_bytes = file_handle.read(bytes_to_read)
        if not chunk_bytes:
            raise EmptyFileError

    raw_floats = np.frombuffer(chunk_bytes, dtype=np.float32)
    i_data = raw_floats[0::2]
    q_data = raw_floats[1::2]
    if len(i_data) < fft_size:
        return None
    return i_data + 1j * q_data


def compute_power_spectrum(iq_complex, window):
    """Linear (not dB) power spectrum of one FFT frame, normalized to dBFS.

    Kept in linear units so callers can accumulate a running average of
    actual power; averaging dB values directly is biased low (log is
    concave), which flattens peaks and drags the apparent noise floor down.

    The raw FFT magnitude scales with both fft_size and the window's
    coherent gain, so an unnormalized spectrum floats up by tens of dB for
    no physical reason (e.g. ~30 dB too high at fft_size=4096 with a
    Hamming window) and drifts if either changes. Dividing by sum(window)
    cancels both effects, so a full-scale tone reads ~0 dB regardless of
    fft_size or window choice, matching the plot's -80..20 dB range.
    """
    windowed_iq = iq_complex * window
    fft_data = np.fft.fft(windowed_iq)
    fft_shifted = np.fft.fftshift(fft_data)
    magnitude = np.abs(fft_shifted) / np.sum(window)
    return magnitude ** 2


def power_to_db(power_spectrum):
    return 10 * np.log10(power_spectrum + 1e-12)


def freq_axis(fft_size, sample_rate_hz):
    return np.fft.fftshift(np.fft.fftfreq(fft_size, d=1.0 / sample_rate_hz))


def estimate_snr_db(avg_psd, freqs, f_min, f_max):
    in_region = (freqs >= f_min) & (freqs <= f_max)
    if not np.any(in_region):
        return None
    signal_db = np.max(avg_psd[in_region])
    outside_psd = avg_psd[~in_region]
    noise_floor_db = np.median(outside_psd) if len(outside_psd) else avg_psd.min()
    return signal_db - noise_floor_db


def estimate_symbol_rate_hz(iq_path, sample_rate_hz, fc_hz, rough_bw_hz, n_samples=1 << 20):
    raw = np.fromfile(iq_path, dtype=np.complex64, count=n_samples)
    return _estimate_symbol_rate_from_array(raw, sample_rate_hz, fc_hz, rough_bw_hz)


def estimate_symbol_rate_hz_from_iq(iq_complex, sample_rate_hz, fc_hz, rough_bw_hz):
    """Same estimator as estimate_symbol_rate_hz, but on an in-memory IQ
    array (e.g. an accumulated real-time buffer) instead of a file path."""
    return _estimate_symbol_rate_from_array(
        np.asarray(iq_complex, dtype=np.complex64), sample_rate_hz, fc_hz, rough_bw_hz
    )

def _estimate_symbol_rate_from_array(raw, sample_rate_hz, fc_hz, rough_bw_hz):
    if len(raw) < 4096:
        raise InsufficientSamplesError

    # Mix the carrier of interest down to baseband
    n = np.arange(len(raw))
    mixed = raw * np.exp(-1j * 2 * np.pi * fc_hz * n / sample_rate_hz)

    # Rough brick-wall filter to isolate it from other carriers
    X = np.fft.fft(mixed)
    freqs_full = np.fft.fftfreq(len(mixed), d=1.0 / sample_rate_hz)
    half_bw = max(rough_bw_hz * 1.5, 750e3)
    X[np.abs(freqs_full) > half_bw] = 0
    filtered = np.fft.ifft(X)

    # Instantaneous power strips modulation, exposing the symbol-rate ripple
    power = np.abs(filtered) ** 2
    power = power - power.mean()

    # FFT of the power waveform - the peak IS the symbol rate
    P = np.abs(np.fft.fft(power))
    freqs_p = np.fft.fftfreq(len(power), d=1.0 / sample_rate_hz)

    min_rs, max_rs = 100e3, min(sample_rate_hz / 2, 10e6)
    mask = (freqs_p > min_rs) & (freqs_p < max_rs)
    if not np.any(mask):
        return None
    candidate_idx = np.where(mask)[0]
    peak_idx = candidate_idx[np.argmax(P[candidate_idx])]
    return freqs_p[peak_idx]
