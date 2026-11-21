"""Step 4: rendering and audio analysis."""

from __future__ import annotations

import numpy as np
import pytest

from reaper_mcp import analysis

from .fake_reaper import write_wav
from .test_editing import Caller, error_text

RATE = 48000


@pytest.fixture
def call(running):
    return Caller(running)


def sine(db, seconds=10.0, freq=1000.0, rate=RATE):
    t = np.arange(int(seconds * rate)) / rate
    return 10 ** (db / 20) * np.sin(2 * np.pi * freq * t)


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_loudness_matches_ebu_reference(tmp_path, rate):
    # EBU Tech 3341 case 1: a stereo 1 kHz sine at -23 dBFS per channel reads -23.0 LUFS.
    tone = sine(-23, rate=rate)
    path = tmp_path / "ref.wav"
    write_wav(path, np.column_stack([tone, tone]), rate)
    report = analysis.analyze(path)
    assert report["loudness"]["integrated_lufs"] == pytest.approx(-23.0, abs=0.1)
    assert report["loudness"]["loudness_range_lu"] == pytest.approx(0, abs=0.2)
    assert report["levels"]["sample_peak_dbfs"] == pytest.approx(-23, abs=0.05)
    assert report["levels"]["crest_factor_db"] == pytest.approx(3.01, abs=0.05)
    assert report["stereo"]["correlation"] == pytest.approx(1.0)
    assert report["spectrum_db_relative"]["mid"] == pytest.approx(0, abs=0.1)


def test_gating_ignores_silence_and_reports_range(tmp_path):
    # EBU 3341 case 3 (-36 / -23 / -36 dB for 10 / 60 / 10 s) with silence in between: the relative
    # gate drops the quiet parts and the absolute gate drops the silence.
    x = np.concatenate([sine(-36, 10), np.zeros(10 * RATE), sine(-23, 60), np.zeros(10 * RATE), sine(-36, 10)])
    path = tmp_path / "gated.wav"
    write_wav(path, np.column_stack([x, x]), RATE)
    report = analysis.analyze(path)
    assert report["loudness"]["integrated_lufs"] == pytest.approx(-23.0, abs=0.2)
    assert report["silent_fraction"] > 0.15
    assert report["loudness"]["loudness_range_lu"] > 5


def test_true_peak_catches_intersample_overs_and_phase_problems(tmp_path):
    # fs/4 sine sampled 45 degrees off its peaks: samples at 0.707, real peak at 1.0 (+3 dB).
    n = np.arange(RATE)
    x = np.sin(np.pi / 2 * n + np.pi / 4)
    path = tmp_path / "isp.wav"
    write_wav(path, np.column_stack([x * 0.99, -x * 0.99]), RATE, bits=16)
    report = analysis.analyze(path)
    assert report["levels"]["true_peak_dbtp"] > report["levels"]["sample_peak_dbfs"] + 2.5
    assert report["stereo"]["correlation"] == pytest.approx(-1.0)


async def test_render_master_mix_and_measure(call):
    fake = call.fake
    fake.project_info.update({"RENDER_FILE": "C:/user/renders", "RENDER_SETTINGS": 1.0})
    result = await call("render", start=0.0, end=4.0, name="mix")
    (path,) = result["files"]
    assert path.endswith("mix.wav")
    (job,) = fake.renders
    assert job["RENDER_SETTINGS"] == 0 and job["RENDER_STARTPOS"] == 0 and job["RENDER_ENDPOS"] == 4
    assert job["RENDER_FORMAT"] == "ZXZhdxgAAQ==" and job["RENDER_BOUNDSFLAG"] == 0
    (report,) = result["analysis"]
    assert report["duration"] == pytest.approx(4.0)
    assert report["loudness"]["integrated_lufs"] == pytest.approx(-20, abs=0.2)
    # The user's own render settings are back.
    assert fake.project_info["RENDER_FILE"] == "C:/user/renders" and fake.project_info["RENDER_SETTINGS"] == 1.0


async def test_render_stems_restores_track_selection(call):
    fake = call.fake
    keys = next(t for t in fake.tracks if t.name == "Keys")
    keys.selected = True
    result = await call("render", start=0.0, end=1.0, tracks=["Drums", "Kick"], name="stem", analyze=False)
    assert [p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for p in result["files"]] == ["stem-Drums.wav", "stem-Kick.wav"]
    assert fake.renders[0]["RENDER_SETTINGS"] == 2 and "analysis" not in result
    assert [t.name for t in fake.tracks if t.selected] == ["Keys"]

    await call("render", start=0.0, end=1.0, tracks=["Drums"], through_master=True, analyze=False)
    assert fake.renders[1]["RENDER_SETTINGS"] == 128


async def test_render_rejects_empty_range_and_analyze_reports_bad_files(call, tmp_path):
    assert "end must be after start" in await error_text(call, "render", start=2.0, end=1.0)
    bad = tmp_path / "x.wav"
    bad.write_bytes(b"not audio")
    assert "not a WAV file" in await error_text(call, "analyze_audio_file", path=str(bad))


def test_install_bridge_autostart_is_idempotent_and_reversible(tmp_path, capsys):
    from reaper_mcp.cli import STARTUP_BEGIN, main

    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "__startup.lua").write_text("-- my own startup stuff\nreaper.ShowConsoleMsg('hi')\n", encoding="utf-8")
    for _ in range(2):
        assert main(["install-bridge", "--resource-path", str(tmp_path), "--autostart"]) == 0
    text = (scripts / "__startup.lua").read_text(encoding="utf-8")
    assert text.count(STARTUP_BEGIN) == 1 and text.startswith("-- my own startup stuff")
    assert (scripts / "reaper_mcp" / "reaper_mcp_bridge.lua").exists()

    assert main(["install-bridge", "--resource-path", str(tmp_path), "--no-autostart"]) == 0
    assert (scripts / "__startup.lua").read_text(encoding="utf-8") == "-- my own startup stuff\nreaper.ShowConsoleMsg('hi')\n"
