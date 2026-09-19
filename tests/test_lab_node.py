"""Node.js Lab runner (spec B, Phase 3): the same built-in presets, run by a Node.js worker with the same protocol."""

import json
import subprocess

import pytest
from sqlalchemy import select

from profiler_service.db import make_engine, make_sessionmaker
from profiler_service.lab_store import LabStore
from profiler_service.models import Run, TrialResult
from vayunx_lab.node_runtime import node_executable, node_info, worker_path
from vayunx_lab.presets import PresetError, get_preset, split_variant
from vayunx_lab.runner import ExperimentRunner
from vayunx_lab.security import security_note
from vayunx_profiler_sdk.histogram import LatencyHistogram, bucket_index

needs_node = pytest.mark.skipif(node_executable() is None, reason="Node.js is not installed")


def test_node_variants_are_derived_from_the_allow_list():
    p = get_preset("argon2id-owasp@node")
    assert (p.id, p.runtime, p.base_id) == ("argon2id-owasp@node", "node", "argon2id-owasp")
    assert p.params == get_preset("argon2id-owasp").params and "Node.js" in p.label
    assert security_note(p) == security_note(get_preset("argon2id-owasp"))
    assert split_variant("md5@node") == ("md5", "node") and split_variant("md5") == ("md5", "python")
    for bad in ("nope@node", "md5@ruby", "md5@", "@node", "md5@node@node"):
        with pytest.raises(PresetError):
            get_preset(bad)


def run_worker(*args, timeout=120):
    r = subprocess.run([node_executable(), str(worker_path()), *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, [json.loads(line) for line in r.stdout.splitlines() if line.strip()], r.stderr


@needs_node
def test_node_worker_speaks_the_lab_protocol():
    code, events, err = run_worker("--preset", "md5", "--duration", "0.3", "--warmup", "0.1")
    assert code == 0, err
    assert [e["event"] for e in events] == ["ready", "measure_start", "measure_end", "result"]
    r = events[-1]
    assert r["preset"] == "md5" and r["algorithm"] == "MD5" and r["concurrency"] == 1
    assert r["library"].startswith("node:crypto") and r["env"]["runtime"].startswith("node ")
    h = LatencyHistogram.from_dict(r["histogram"])
    assert h.count == r["ops"] == sum(h.buckets.values()) and r["ops"] > 1000
    assert h.min_ns <= r["p50_ns"] <= h.max_ns and r["percentile_method"] == "exact"
    assert bucket_index(h.min_ns) == min(h.buckets) and bucket_index(h.max_ns) == max(h.buckets)
    for key in ("cpu_s_per_op", "cores_busy", "rss_before_bytes", "peak_rss_bytes", "timer_overhead_ns", "wall_s"):
        assert r[key] is not None and r[key] >= 0, key


@needs_node
def test_node_worker_runs_password_hash_presets_with_the_same_parameters():
    code, events, err = run_worker("--preset", "argon2id-owasp", "--duration", "0.2", "--warmup", "0")
    assert code == 0, err
    r = events[-1]
    assert r["params"] == "m=19456,t=2,p=1" and r["ops"] >= 10 and r["p50_ns"] > 1_000_000  # min_ops, and > 1 ms


@needs_node
def test_node_worker_refuses_unknown_presets_and_bad_arguments():
    code, events, _ = run_worker("--preset", "rm -rf /", "--duration", "1")
    assert code == 2 and events[-1]["event"] == "error" and "unknown preset" in events[-1]["error"]
    code, events, _ = run_worker("--preset", "md5", "--duration", "999")
    assert code == 2 and "duration" in events[-1]["error"]
    code, events, _ = run_worker("--preset", "md5", "--duration", "1", "--concurrency", "4")
    assert code == 2 and "concurrency" in events[-1]["error"]


@needs_node
def test_experiment_compares_python_and_node_side_by_side(tmp_path, db_url):
    Session = make_sessionmaker(make_engine(db_url(tmp_path)))
    runner = ExperimentRunner(LabStore(Session), ["sha256", "sha256@node"], trials=1, duration_s=0.3, warmup_s=0.1,
                              quiet_max_wait_s=0)
    exp_id = runner.create()
    assert runner.run(exp_id) == "complete"
    with Session() as s:
        rows = s.scalars(select(TrialResult).where(TrialResult.experiment_id == exp_id)).all()
        assert sorted(r.preset_id for r in rows) == ["sha256", "sha256@node"]
        node = next(r for r in rows if r.preset_id == "sha256@node")
        meta = json.loads(s.get(Run, node.run_id).metadata_json)
        assert meta["library"].startswith("node:crypto") and meta["runtime"].startswith("node ") and node.ops > 1000


def test_node_variants_need_concurrency_1():
    with pytest.raises(ValueError, match="concurrency 1"):
        ExperimentRunner(None, ["md5", "md5@node"], concurrency=2)


@needs_node
def test_node_info_is_measured_not_assumed():
    info = node_info()
    assert info["version"].startswith("v") and info["openssl"]
