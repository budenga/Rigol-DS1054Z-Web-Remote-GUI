"""Analysis engine tests. Golden values come from a real 24 V capture (tests/fixtures)."""

import io
import math
from pathlib import Path

import numpy as np
import pytest

from rigol import analysis as an
from rigol.waveform import WaveformPreamble

FIXTURE = Path(__file__).parent / "fixtures" / "DS1054Z_data_20261118_103155.csv"


def pre(yinc=0.1, yorig=0, yref=0, points=0, xinc=1e-3, xorig=0.0):
    return WaveformPreamble(0, 0, points, 1, xinc, xorig, 0, yinc, yorig, yref)


def synth(v, i, dt=1e-3, t0=0.0):
    return an.from_arrays(1, 0.0, "csv", 1, 4, t0, dt, v, i)


def full(cap):
    return cap.t_start, cap.t_end


# --- golden values --------------------------------------------------------

def test_fixture_golden_values():
    cap = an.load_app_csv(FIXTURE.read_text(), 1, 4)
    assert cap.n == 1200
    assert cap.dt == pytest.approx(1e-3, rel=1e-9)
    s = an.window_stats(cap, *full(cap))
    assert s["n_window"] == 1200
    assert s["dt_window"] == pytest.approx(1.2, rel=1e-9)
    assert s["v_avg"] == pytest.approx(22.752, rel=1e-3)
    assert s["i_avg"] == pytest.approx(1.1978, rel=1e-3)
    assert s["p_avg"] == pytest.approx(26.966, rel=1e-3)
    assert s["i_rms"] == pytest.approx(1.983, rel=1e-3)
    # reference values computed independently from the CSV (plain Python, no numpy)
    assert s["v_rms"] == pytest.approx(22.7563, rel=1e-4)
    assert s["p_rms"] == pytest.approx(44.0516, rel=1e-4)
    # Prms is not Vrms * Irms (that would be apparent power, 45.12 VA here)
    assert s["p_rms"] != pytest.approx(s["v_rms"] * s["i_rms"], rel=1e-3)
    assert s["energy_j"] == pytest.approx(32.36, rel=1e-3)
    assert s["n_clipped_v"] == 0 and s["n_clipped_i"] == 0
    # p_avg is mean(v*i), not v_avg*i_avg
    assert s["p_avg"] != pytest.approx(s["v_avg"] * s["i_avg"], rel=1e-4)
    assert s["energy_wh"] == pytest.approx(s["energy_j"] / 3600)
    assert s["energy_mwh"] == pytest.approx(s["energy_j"] / 3.6)
    assert s["charge_mah"] == pytest.approx(s["i_avg"] * 1.2 / 3.6)


def test_fixture_missing_channel_lists_present():
    with pytest.raises(an.ChannelsMissing) as e:
        an.load_app_csv(FIXTURE.read_text(), 1, 3)
    assert e.value.present == [1, 4]


# --- synthetic ------------------------------------------------------------

def test_constant_power_energy_charge():
    n = 500
    cap = synth(np.full(n, 10.0), np.full(n, 2.0))
    s = an.window_stats(cap, *full(cap))
    assert s["p_avg"] == pytest.approx(20.0)
    assert s["dt_window"] == pytest.approx(0.5)
    assert s["energy_j"] == pytest.approx(10.0)
    assert s["charge_mah"] == pytest.approx(2 * 0.5 / 3.6)
    assert s["v_rms"] == pytest.approx(10.0)
    assert s["p_rms"] == pytest.approx(20.0)
    assert s["crest_i"] == pytest.approx(1.0)


def test_square_pulses():
    i = np.zeros(1000)
    for a in (100, 400, 700):
        i[a:a + 50] = 5.0
    i[400:400 + 80] = 5.0           # second pulse is longer: 80 samples
    v = np.full(1000, 24.0)
    cap = synth(v, i)
    s = an.window_stats(cap, *full(cap), cfg=an.AnalysisConfig(i_threshold=1.0))
    assert s["n_pulses"] == 3
    assert s["t_above"] == pytest.approx((50 + 80 + 50) * 1e-3)
    assert s["t_pulse_max"] == pytest.approx(80e-3)


def test_hysteresis_ignores_ripple_on_threshold():
    t = np.arange(2000)
    i = np.where(t > 1000, 5.0, 0.0).astype(np.float64)
    i += np.where(t % 2 == 0, 0.02, -0.02) * (np.abs(t - 1000) < 4)   # chatter at the edge
    s = an.window_stats(synth(np.full(2000, 20.0), i), 0, 10, cfg=an.AnalysisConfig(1.0))
    assert s["n_pulses"] == 1


def test_clipped_samples_are_nan_excluded_and_counted():
    raw_v = np.full(100, 150, np.uint8)
    raw_i = np.full(100, 110, np.uint8)
    raw_i[10] = 255
    raw_i[20:23] = 0
    raw_v[30] = 0
    pv, pi_ = pre(yinc=0.2, yorig=0, yref=100), pre(yinc=0.1, yorig=0, yref=100)
    cap = an._build(1, 0.0, "raw", 1, 4, 0.0, 1e-3, raw_v, raw_i, pv, pi_, {}, {})
    assert cap.i_clip == 4 and cap.v_clip == 1
    assert np.isnan(cap.i[10]) and np.isnan(cap.v[30])
    s = an.window_stats(cap, *full(cap))
    assert s["n_clipped_i"] == 4 and s["n_clipped_v"] == 1
    assert s["i_avg"] == pytest.approx(1.0)       # (110-100)*0.1, NaNs excluded
    assert s["v_avg"] == pytest.approx(10.0)
    assert s["n_valid"] == 100 - 5
    assert s["p_avg"] == pytest.approx(10.0)


def test_all_clipped_channel_does_not_crash():
    cap = synth(np.full(50, np.nan), np.full(50, 1.0))
    s = an.window_stats(cap, *full(cap))
    assert s["v_avg"] is None and s["p_avg"] is None and s["energy_j"] is None
    assert s["i_avg"] == pytest.approx(1.0)
    assert s["n_clipped_v"] == 50
    env = an.envelope(cap, *full(cap), 20)
    assert env["mode"] in ("samples", "envelope")


def test_window_edges():
    cap = synth(np.full(1000, 10.0), np.full(1000, 2.0))
    a = an.window_stats(cap, 0.2, 0.1)
    b = an.window_stats(cap, 0.1, 0.2)
    assert a == b and a["n_window"] == 101       # swapped t0 > t1 is accepted
    out = an.window_stats(cap, 5.0, 6.0)
    assert out["n_window"] == 0 and out["p_avg"] is None and out["dt_window"] == 0.0
    out = an.window_stats(cap, -3.0, -2.0)
    assert out["n_window"] == 0
    # window larger than data is clamped
    assert an.window_stats(cap, -1, 99)["n_window"] == 1000
    # single sample, NaN bounds
    assert an.window_stats(cap, 0.5, 0.5)["n_window"] == 1
    assert an.window_stats(cap, float("nan"), 1.0)["n_window"] == 0
    assert an.envelope(cap, 5, 6, 100)["mode"] == "empty"


def test_r_app_recovers_source_resistance():
    rng = np.random.default_rng(0)
    i = rng.uniform(0, 10, 5000)
    v = 24.0 - 0.15 * i
    s = an.window_stats(synth(v, i), 0, 10)
    assert s["r_app"] == pytest.approx(0.15, rel=1e-4)
    flat = an.window_stats(synth(np.full(100, 24.0), np.full(100, 1.0)), 0, 1)
    assert flat["r_app"] is None


def test_v_rest_and_drop():
    i = np.full(1000, 0.3)
    i[500:520] = 8.0
    v = np.full(1000, 24.0)
    v[500:520] = 22.0
    s = an.window_stats(synth(v, i), 0, 10, cfg=an.AnalysisConfig(1.0))
    assert s["v_rest"] == pytest.approx(24.0)
    assert s["v_drop"] == pytest.approx(2.0)


def test_large_capture_precision_float32():
    n = 3_000_000
    cap = synth(np.full(n, 24.0), np.full(n, 0.3), dt=1e-6)
    s = an.window_stats(cap, *full(cap))
    assert s["p_avg"] == pytest.approx(7.2, rel=1e-6)
    assert s["energy_j"] == pytest.approx(7.2 * 3.0, rel=1e-6)


# --- envelope -------------------------------------------------------------

def test_envelope_keeps_narrow_peak():
    n = 100_000
    i = np.full(n, 0.3)
    i[54321] = 12.0
    cap = synth(np.full(n, 24.0), i, dt=1e-5)
    env = an.envelope(cap, *full(cap), 800)
    assert env["mode"] == "envelope" and len(env["i_max"]) == 800
    assert max(env["i_max"]) == pytest.approx(12.0)
    assert max(env["p_max"]) == pytest.approx(288.0)
    assert min(env["i_min"]) == pytest.approx(0.3, rel=1e-4)


def test_envelope_samples_when_few():
    cap = synth(np.arange(10, dtype=float), np.ones(10))
    env = an.envelope(cap, *full(cap), 100)
    assert env["mode"] == "samples" and env["n"] == 10 and len(env["t"]) == 10


# --- I/O ------------------------------------------------------------------

def test_npz_round_trip_bit_exact(tmp_path):
    rng = np.random.default_rng(3)
    raw_v = rng.integers(0, 256, 5000, dtype=np.uint8)
    raw_i = rng.integers(0, 256, 5000, dtype=np.uint8)
    cap = an._build(7, 123.5, "raw", 1, 4, -0.5, 2e-6, raw_v, raw_i,
                    pre(0.2, -100, 125), pre(0.2, -50, 125), {"VAVG,CHAN1": 22.7, "X": None},
                    {"srate": 5e5})
    path = tmp_path / "c.npz"
    an.save_npz(cap, path)
    back = an.load_npz(path)
    assert np.array_equal(back.v, cap.v, equal_nan=True)
    assert np.array_equal(back.i, cap.i, equal_nan=True)
    assert back.v.tobytes() == cap.v.tobytes()
    assert (back.t0, back.dt, back.n, back.v_ch, back.i_ch) == (-0.5, 2e-6, 5000, 1, 4)
    assert back.scope_params == {"VAVG,CHAN1": 22.7, "X": None}
    assert back.v_clip == cap.v_clip and back.i_clip == cap.i_clip


def test_npz_float_only_capture_round_trip(tmp_path):
    cap = synth(np.array([1.0, np.nan, 3.0], np.float32), np.array([1, 2, 3], np.float32))
    path = tmp_path / "f.npz"
    an.save_npz(cap, path)
    back = an.load_npz(path)
    assert np.array_equal(back.v, cap.v, equal_nan=True)


@pytest.mark.parametrize("payload", [b"not a zip", b""])
def test_npz_garbage_is_a_clear_error(payload):
    with pytest.raises(an.AnalysisError):
        an.load_npz(io.BytesIO(payload))


def test_npz_foreign_file_is_a_clear_error(tmp_path):
    path = tmp_path / "foreign.npz"
    np.savez(path, something=np.arange(3))
    with pytest.raises(an.AnalysisError, match="not saved by this app"):
        an.load_npz(path)
    path2 = tmp_path / "old.npz"
    np.savez(path2, meta_json=np.array('{"version": 99}'))
    with pytest.raises(an.AnalysisError, match="version"):
        an.load_npz(path2)


@pytest.mark.parametrize("text, match", [
    ("", "empty"),
    ("a,b,c\n1,2,3\n", "Unrecognised"),
    ("Channel,Sample,Raw,Volts,Time_s\n", "no data"),
    ("Channel,Sample,Raw,Volts,Time_s\nCH1,0,1,x,0\n", "malformed"),
])
def test_csv_malformed(text, match):
    with pytest.raises(an.AnalysisError, match=match):
        an.load_app_csv(text, 1, 4)


def test_csv_nonuniform_time_rejected():
    rows = ["Channel,Sample,Raw,Volts,Time_s"]
    for ch in (1, 4):
        for k, t in enumerate([0, 1e-3, 5e-3, 6e-3]):
            rows.append(f"CH{ch},{k},100,1.0,{t}")
    with pytest.raises(an.AnalysisError, match="uniformly"):
        an.load_app_csv("\n".join(rows), 1, 4)


def test_window_csv_export_and_reimport():
    cap = an.load_app_csv(FIXTURE.read_text(), 1, 4)
    text = an.export_window_csv(cap, -0.1, 0.1)
    lines = text.strip().splitlines()
    assert lines[0] == "t_s,v,i,p" and len(lines) == 1 + 201
    back = an.load_app_csv(text, 1, 4)
    s1 = an.window_stats(cap, -0.1, 0.1)
    s2 = an.window_stats(back, back.t_start, back.t_end)
    assert s2["p_avg"] == pytest.approx(s1["p_avg"], rel=1e-5)


def test_csv_export_refuses_huge_window():
    cap = synth(np.zeros(2_100_000), np.zeros(2_100_000), dt=1e-6)
    with pytest.raises(an.WindowTooLarge):
        an.export_window_csv(cap, *full(cap))
    ok = an.export_window_csv(cap, 0.0, 0.0001)
    assert ok.count("\n") == 102


def test_clipped_samples_are_blank_in_csv():
    cap = synth(np.array([1.0, np.nan, 3.0]), np.array([1.0, 2.0, 3.0]))
    rows = an.export_window_csv(cap, *full(cap)).strip().splitlines()
    assert rows[2].split(",")[1:] == ["", "2.000000e+00", ""]


def test_envelope_never_emits_inf():
    v = np.full(5000, 1.0e30, np.float32)
    i = np.full(5000, 1.0e30, np.float32)           # v*i overflows float32 -> inf
    cap = synth(v, i)
    env = an.envelope(cap, *full(cap), 100)
    import json
    json.dumps(env, allow_nan=False)                # must be valid JSON
    assert all(x is None for x in env["p_max"])


def test_weak_pulse_above_threshold_is_counted_like_t_above():
    i = np.array([0.3, 1.2, 1.2, 0.3, 12, 0.3])
    s = an.window_stats(synth(np.full(6, 24.0), i), 0, 1, cfg=an.AnalysisConfig(1.0))
    assert s["t_above"] == pytest.approx(3e-3)
    assert s["n_pulses"] == 2 and s["t_pulse_max"] == pytest.approx(2e-3)


def test_huge_window_bounds_do_not_overflow():
    cap = synth(np.full(100, 1.0), np.full(100, 1.0))
    assert an.window_stats(cap, -1e308, 1e308)["n_window"] == 100
    assert an.envelope(cap, -1e308, 1e308, 10)["n"] == 100
    assert an.export_window_csv(cap, -1e308, 1e308).count("\n") == 101


def test_csv_with_large_offset_and_small_step_round_trips():
    rows = ["Channel,Sample,Raw,Volts,Time_s"]
    for ch in (1, 4):
        for k in range(1200):
            rows.append(f"CH{ch},{k},100,1.0,{0.01 + k * 2e-9:.11e}")
    cap = an.load_app_csv("\n".join(rows), 1, 4)
    assert cap.dt == pytest.approx(2e-9, rel=1e-6)


def test_npz_with_bad_channels_or_shapes_is_rejected(tmp_path):
    import json
    base = {"version": 1, "id": 1, "ts": 0.0, "source": "raw", "t0": 0.0, "dt": 1e-3, "n": 4,
            "scope_params": {}, "meta": {}}
    def write(name, meta, v, i):
        p = tmp_path / name
        np.savez(p, meta_json=np.array(json.dumps(meta)), v=v, i=i)
        return p
    ok = np.ones(4, np.float32)
    with pytest.raises(an.AnalysisError, match="channel"):
        an.load_npz(write("a.npz", {**base, "v_ch": 9, "i_ch": 4}, ok, ok))
    with pytest.raises(an.AnalysisError, match="one-dimensional"):
        an.load_npz(write("b.npz", {**base, "v_ch": 1, "i_ch": 4}, np.ones((2, 3), np.float32), ok))
    with pytest.raises(an.AnalysisError):
        an.load_npz(write("c.npz", {**base, "v_ch": 1, "i_ch": 4, "dt": "abc"}, ok, ok))


def test_p_rms_is_rms_of_instantaneous_power():
    # 50 % duty: p = 0 or 100 W -> Pavg 50 W, Prms = sqrt(0.5 * 100^2) = 70.71 W
    n = 1000
    v = np.full(n, 10.0)
    i = np.tile([0.0, 10.0], n // 2)
    cap = synth(v, i)
    s = an.window_stats(cap, *full(cap))
    assert s["p_avg"] == pytest.approx(50.0)
    assert s["p_rms"] == pytest.approx(100.0 / np.sqrt(2))
