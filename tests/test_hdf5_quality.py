import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
h5py = pytest.importorskip("h5py")
SPEC = importlib.util.spec_from_file_location(
    "quality", Path(__file__).resolve().parents[1] / "scripts" / "check_hdf5_quality.py"
)
quality = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quality)


def test_third_difference_and_derivative_of_cubic():
    t = np.arange(12, dtype=np.int64)
    result = quality.analyze_signal(1_750_000_000_000_000_000 + t * 1_000_000_000, t.astype(float) ** 3)
    third = result["levels"][3]
    np.testing.assert_allclose(third["raw_difference"], 6)
    np.testing.assert_allclose(third["time_derivative"], 6)
    np.testing.assert_allclose(third["time_s"], np.arange(1.5, 10))
    np.testing.assert_array_equal(third["derivative_z"], 0)


def test_actual_time_step_and_nanosecond_precision():
    ns = np.arange(20, dtype=np.int64) * 10_000_000 + 1_750_000_000_000_000_000
    result = quality.analyze_signal(ns, np.arange(20) * 0.02)
    np.testing.assert_allclose(result["levels"][1]["raw_difference"], 0.02)
    np.testing.assert_allclose(result["levels"][1]["time_derivative"], 2)
    assert result["summary"]["timing"]["median_rate_hz"] == pytest.approx(100)


def test_nonuniform_linear_function():
    t = np.array([0, 1, 3, 4, 7, 9], dtype=np.int64)
    levels = quality.analyze_signal(t * 1_000_000_000, t * 3.0 + 2)["levels"]
    np.testing.assert_allclose(levels[1]["time_derivative"], 3)
    np.testing.assert_allclose(levels[2]["time_derivative"], 0)
    assert not np.all(levels[1]["raw_difference"] == 3)


@pytest.mark.parametrize("values", [np.zeros(8), np.ones(8), np.ones(8) * 1e300])
def test_constant_z_is_zero(values):
    z, stats = quality.z_score(values)
    assert stats["constant"]
    np.testing.assert_array_equal(z, 0)


def test_z_ignores_nonfinite_values_and_uses_population_std():
    z, stats = quality.z_score(np.array([1, 2, 3, np.nan, np.inf]))
    assert stats["count"] == 3
    assert stats["std"] == pytest.approx(np.sqrt(2 / 3))
    assert np.isnan(z[3:]).all()
    assert np.std(z[:3]) == pytest.approx(1)


def test_impulse_is_flagged_through_order_three():
    values = np.zeros(200)
    values[100] = 20
    result = quality.analyze_signal(np.arange(200, dtype=np.int64) * 10_000_000, values)
    for level in result["levels"]:
        assert level["stats"]["raw_difference"]["flagged_count"] > 0


def test_do_not_bridge_duplicate_backwards_missing_values_or_gaps():
    t = np.array([0, 1, 1, 3, 2, 100, 101, 102, 103, 104], dtype=np.int64)
    x = t.astype(float)
    x[7] = np.nan
    result = quality.analyze_signal(t * 1_000_000_000, x)
    stats = result["summary"]["timing"]
    assert stats["duplicate_intervals"] == 1
    assert stats["backward_intervals"] == 1
    assert stats["gap_intervals"] == 1
    d = result["levels"][1]["time_derivative"]
    assert np.isnan(d[[1, 3, 4, 6, 7]]).all()
    assert result["summary"]["errors"]
    for level in result["levels"][2:]:
        for i, value in enumerate(level["time_derivative"]):
            if np.isfinite(value):
                assert np.isfinite(d[i:i + level["order"]]).all()


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_short_signals_do_not_crash(count):
    result = quality.analyze_signal(np.arange(count, dtype=np.int64), np.ones(count))
    assert len(result["levels"]) == 4
    assert len(result["levels"][3]["raw_difference"]) == max(0, count - 3)


@pytest.mark.parametrize("timestamps,values", [
    (np.array([0, 1]), np.array([1])),
    (np.array([0.0, 1.0]), np.array([1, 2])),
    (np.array([0, 1]), np.ones((2, 2))),
    (np.array([0, 2**64 - 1], dtype=np.uint64), np.array([1, 2])),
])
def test_malformed_signal_rejected(timestamps, values):
    with pytest.raises(ValueError):
        quality.analyze_signal(timestamps, values)


def make_file(path):
    with h5py.File(path, "w") as handle:
        handle.attrs["complete"] = True
        signals = handle.create_group("signals")
        for i, axis in enumerate("xyzw"):
            group = signals.create_group(str(i))
            group.attrs["key"] = "head.orientation." + axis
            group.create_dataset("timestamps_ns", data=np.arange(100, dtype=np.int64) * 10_000_000)
            values = np.zeros(100)
            if axis == "w":
                values[:] = 1
                values[50:] = -1  # same orientation, opposite quaternion sign
            group.create_dataset("values", data=values)
        video = handle.create_group("videos/left_camera")
        video.create_dataset("timestamps_ns", data=np.arange(10, dtype=np.int64) * 33_333_333)
        video.create_dataset("frame_indices", data=np.arange(10))
        handle.create_group("calibration").attrs["left_encoder_offset_rad"] = 1.25


def test_report_outputs_source_unchanged_and_npz_readable(tmp_path):
    source = tmp_path / "source.h5"
    make_file(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    out = tmp_path / "report"
    report = quality.build_report(source, out)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert report["summary"]["signal_count"] == 4
    assert report["quaternions"][0]["possible_sign_flips"] == 1
    assert report["quaternions"][0]["norm_outside_tolerance"] == 0
    assert report["calibration"]["left_encoder_offset_rad"] == 1.25
    assert {p.name for p in out.iterdir()} == {"report.html", "summary.json", "anomalies.csv", "series.npz"}
    parsed = json.loads((out / "summary.json").read_text())
    assert parsed["settings"]["max_order"] == 3
    with np.load(out / "series.npz", allow_pickle=False) as data:
        assert len(data["signal_0000/order_3/raw_z"]) == 97
    assert "https://" not in (out / "report.html").read_text()
    with pytest.raises(FileExistsError):
        quality.build_report(source, out)


def test_bad_channel_does_not_hide_other_channels(tmp_path):
    source = tmp_path / "source.h5"
    make_file(source)
    with h5py.File(source, "r+") as handle:
        del handle["signals/0/values"]
    report = quality.build_report(source, tmp_path / "report")
    assert report["summary"]["error_signals"] == 1
    assert len(report["signals"]) == 4
    assert report["quaternions"][0].get("warning")


def test_html_escapes_untrusted_names(tmp_path):
    source = tmp_path / "source.h5"
    make_file(source)
    with h5py.File(source, "r+") as handle:
        handle["signals/0"].attrs["key"] = '</script><img src=x onerror=alert(1)>'
    quality.build_report(source, tmp_path / "report")
    text = (tmp_path / "report/report.html").read_text()
    assert '</script><img src=x' not in text
    assert r'\u003c/script\u003e' in text


def test_plot_downsampling_preserves_breaks_without_false_gaps():
    times = np.arange(10000, dtype=float) / 100
    values = np.sin(times)
    values[3456] = np.nan
    z, _ = quality.z_score(values)
    points = quality.sample_plot(times, values, z, limit=100, max_gap=0.05)
    assert sum(p[3] for p in points) == 2  # first point plus the true missing sample


def test_cli_status_and_no_overwrite(tmp_path, monkeypatch):
    source = tmp_path / "source.h5"
    make_file(source)
    calls = []
    def fake_plots(output, metric):
        calls.append((output, metric))
        return {"status": "complete", "metric": metric, "image_count": 12, "index": "curves/index.html"}
    monkeypatch.setattr(quality, "generate_static_plots", fake_plots)
    assert quality.main([str(source), "--output-dir", str(tmp_path / "a")]) == 0
    assert quality.main([str(source), "--output-dir", str(tmp_path / "b"), "--fail-on-findings"]) == 2
    assert quality.main([str(source), "--output-dir", str(tmp_path / "a")]) == 1
    assert len(calls) == 2
    assert calls[0][1] == "difference"
    assert 'href="curves/index.html"' in (tmp_path / "a/report.html").read_text()
    assert json.loads((tmp_path / "a/summary.json").read_text())["static_plots"]["image_count"] == 12


def test_cli_can_skip_plots_or_select_derivatives(tmp_path, monkeypatch):
    source = tmp_path / "source.h5"
    make_file(source)
    calls = []
    def fake_plots(output, metric):
        calls.append(metric)
        return {"status": "complete", "metric": metric, "image_count": 12, "index": "curves/index.html"}
    monkeypatch.setattr(quality, "generate_static_plots", fake_plots)
    assert quality.main([str(source), "--output-dir", str(tmp_path / "a"), "--no-plots"]) == 0
    assert calls == []
    assert quality.main([str(source), "--output-dir", str(tmp_path / "b"), "--plot-metric", "derivative"]) == 0
    assert calls == ["derivative"]


def test_plot_failure_preserves_report_and_returns_error(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source.h5"
    make_file(source)
    def fail(*args):
        raise RuntimeError("test plotting failure")
    monkeypatch.setattr(quality, "generate_static_plots", fail)
    output = tmp_path / "report"
    assert quality.main([str(source), "--output-dir", str(output)]) == 1
    assert (output / "summary.json").is_file()
    assert (output / "report.html").is_file()
    assert 'href="curves/index.html"' not in (output / "report.html").read_text()
    assert "test plotting failure" in capsys.readouterr().err


def test_plot_interpreter_falls_back_to_system_python(tmp_path, monkeypatch):
    from types import SimpleNamespace
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "-c":
            return SimpleNamespace(returncode=0 if command[0] == "/usr/bin/python3" else 1)
        curves = tmp_path / "curves"
        curves.mkdir()
        (curves / "index.html").write_text("test")
        (curves / "overview_order_0.png").write_bytes(b"test")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(quality.subprocess, "run", run)
    monkeypatch.setattr(quality.sys, "executable", "/different/python")
    monkeypatch.setattr(quality.Path, "is_file", lambda path: True)
    result = quality.generate_static_plots(tmp_path)
    assert [call[0] for call in calls] == ["/different/python", "/usr/bin/python3", "/usr/bin/python3"]
    assert result["image_count"] == 1
