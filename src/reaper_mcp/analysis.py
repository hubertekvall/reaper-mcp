"""Measure a rendered audio file: loudness (ITU-R BS.1770 / EBU R128), peaks, dynamics,
stereo image and tonal balance. Pure numpy/scipy, so it runs anywhere the server runs."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import numpy as np
from scipy import signal

BANDS = [
    ("sub", 20, 60),
    ("bass", 60, 250),
    ("low_mid", 250, 500),
    ("mid", 500, 2000),
    ("high_mid", 2000, 4000),
    ("presence", 4000, 6000),
    ("air", 6000, 20000),
]


class AudioFileError(Exception):
    pass


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Read a PCM (16/24/32-bit) or float (32/64-bit) WAV file into float64 samples (n, channels)."""
    data = Path(path).read_bytes()
    if len(data) < 12 or data[:4] not in (b"RIFF", b"RF64") or data[8:12] != b"WAVE":
        raise AudioFileError(f"{path} is not a WAV file")
    pos, fmt, raw = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            raw = body if size != 0xFFFFFFFF else data[pos + 8:]
            if fmt is not None:
                break
        pos += 8 + size + (size & 1)
    if fmt is None or raw is None:
        raise AudioFileError(f"{path} has no fmt or data chunk")
    tag, channels, rate = struct.unpack("<HHI", fmt[:8])
    bits = struct.unpack("<H", fmt[14:16])[0]
    if tag == 0xFFFE and len(fmt) >= 26:
        tag = struct.unpack("<H", fmt[24:26])[0]
    width = bits // 8
    usable = len(raw) - len(raw) % (width * channels)
    raw = raw[:usable]
    if tag == 3:
        x = np.frombuffer(raw, dtype="<f4" if bits == 32 else "<f8").astype(np.float64)
    elif tag == 1:
        if bits == 16:
            x = np.frombuffer(raw, dtype="<i2") / 32768.0
        elif bits == 24:
            b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            v = np.where(v & 0x800000, v - 0x1000000, v)
            x = v / 8388608.0
        elif bits == 32:
            x = np.frombuffer(raw, dtype="<i4") / 2147483648.0
        elif bits == 8:
            x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128) / 128.0
        else:
            raise AudioFileError(f"unsupported bit depth {bits}")
    else:
        raise AudioFileError(f"unsupported WAV format tag {tag}")
    return x.reshape(-1, channels), rate


def _k_weighting(rate: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """BS.1770 K-weighting (high shelf + high pass), derived for any sample rate."""
    # Stage 1: high shelf
    f0, g, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = np.tan(np.pi * f0 / rate)
    vh = 10 ** (g / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    b1 = np.array([(vh + vb * k / q + k * k) / a0, 2 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0])
    a1 = np.array([1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0])
    # Stage 2: high pass
    f0, q = 38.13547087602444, 0.5003270373238773
    k = np.tan(np.pi * f0 / rate)
    a0 = 1 + k / q + k * k
    b2 = np.array([1.0, -2.0, 1.0])
    a2 = np.array([1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0])
    return [(b1, a1), (b2, a2)]


def _block_loudness(weighted: np.ndarray, rate: int, block_s: float, overlap: float) -> np.ndarray:
    """Loudness of each gating block, in LUFS (channel weights 1.0 for L/R/C)."""
    size = int(round(block_s * rate))
    step = max(1, int(round(size * (1 - overlap))))
    if len(weighted) < size:
        return np.array([])
    sq = np.concatenate([np.zeros((1, weighted.shape[1])), np.cumsum(weighted ** 2, axis=0)])
    starts = np.arange(0, len(weighted) - size + 1, step)
    ms = (sq[starts + size] - sq[starts]) / size
    power = ms.sum(axis=1)
    with np.errstate(divide="ignore"):
        return -0.691 + 10 * np.log10(power)


def _integrated(blocks: np.ndarray) -> float | None:
    above = blocks[blocks > -70]
    if above.size == 0:
        return None
    rel = 10 * np.log10(np.mean(10 ** (above / 10))) - 10
    gated = above[above > rel]
    if gated.size == 0:
        return None
    return float(10 * np.log10(np.mean(10 ** (gated / 10))))


def _db(v: float) -> float | None:
    return round(20 * np.log10(v), 2) if v > 0 else None


def analyze(path: str | Path) -> dict[str, Any]:
    x, rate = read_wav(path)
    n, channels = x.shape
    if n == 0:
        raise AudioFileError(f"{path} contains no audio")
    out: dict[str, Any] = {
        "file": str(path),
        "duration": round(n / rate, 3),
        "sample_rate": rate,
        "channels": channels,
    }

    # Loudness
    weighted = x
    for b, a in _k_weighting(rate):
        weighted = signal.lfilter(b, a, weighted, axis=0)
    momentary = _block_loudness(weighted, rate, 0.4, 0.75)
    short = _block_loudness(weighted, rate, 3.0, 2 / 3)
    lra = None
    if short.size:
        s_abs = short[short > -70]
        if s_abs.size:
            rel = 10 * np.log10(np.mean(10 ** (s_abs / 10))) - 20
            s_gated = s_abs[s_abs > rel]
            if s_gated.size > 1:
                lo, hi = np.percentile(s_gated, [10, 95])
                lra = round(float(hi - lo), 1)
    integrated = _integrated(momentary)
    out["loudness"] = {
        "integrated_lufs": round(integrated, 1) if integrated is not None else None,
        "momentary_max_lufs": round(float(momentary.max()), 1) if momentary.size and np.isfinite(momentary.max()) else None,
        "short_term_max_lufs": round(float(short.max()), 1) if short.size and np.isfinite(short.max()) else None,
        "loudness_range_lu": lra,
    }

    # Peaks and dynamics
    peak = float(np.max(np.abs(x)))
    oversampled = signal.resample_poly(x, 4, 1, axis=0) if n > 32 else x
    true_peak = float(np.max(np.abs(oversampled)))
    rms = float(np.sqrt(np.mean(x ** 2)))
    out["levels"] = {
        "sample_peak_dbfs": _db(peak),
        "true_peak_dbtp": _db(true_peak),
        "rms_dbfs": _db(rms),
        "crest_factor_db": round(20 * np.log10(peak / rms), 2) if rms > 0 and peak > 0 else None,
        "clipped_samples": int(np.sum(np.abs(x) >= 0.999)),
        "dc_offset": [round(float(v), 5) for v in x.mean(axis=0)],
    }

    # Stereo image
    if channels >= 2:
        left, right = x[:, 0], x[:, 1]
        denom = np.sqrt(np.sum(left ** 2) * np.sum(right ** 2))
        mid, side = (left + right) / 2, (left - right) / 2
        mid_rms, side_rms = np.sqrt(np.mean(mid ** 2)), np.sqrt(np.mean(side ** 2))
        l_rms, r_rms = np.sqrt(np.mean(left ** 2)), np.sqrt(np.mean(right ** 2))
        out["stereo"] = {
            "correlation": round(float(np.sum(left * right) / denom), 3) if denom > 0 else None,
            "side_to_mid_db": round(float(20 * np.log10(side_rms / mid_rms)), 1) if side_rms > 0 and mid_rms > 0 else None,
            "balance_db": round(float(20 * np.log10(r_rms / l_rms)), 2) if l_rms > 0 and r_rms > 0 else None,
            "note": "correlation: 1 = mono, 0 = wide, negative = phase problems; balance > 0 leans right",
        }

    # Tonal balance (share of energy per band)
    mono = x.mean(axis=1)
    if np.any(mono):
        freqs, psd = signal.welch(mono, fs=rate, nperseg=min(8192, n))
        total = psd[(freqs >= 20) & (freqs <= 20000)].sum()
        bands = {}
        for name, lo, hi in BANDS:
            e = psd[(freqs >= lo) & (freqs < min(hi, rate / 2))].sum()
            bands[name] = round(float(10 * np.log10(e / total)), 1) if e > 0 and total > 0 else None
        out["spectrum_db_relative"] = bands
        centroid = float(np.sum(freqs * psd) / np.sum(psd))
        out["spectral_centroid_hz"] = round(centroid)

    # Silence: share of 400 ms blocks below -60 LUFS
    if momentary.size:
        out["silent_fraction"] = round(float(np.mean(momentary < -60)), 3)
    return out
