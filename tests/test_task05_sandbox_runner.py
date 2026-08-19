"""
Red-proof tests for TASK-05-SANDBOX-RUNNER.

Hermetic: no real container is ever launched. A fake executor is injected
into SandboxRunner and we assert on the exact argv it receives, plus the
mapping of executor results/exceptions to SandboxResult.
"""
import os
import subprocess

import pytest

from iadf.sandbox.runner import (
    ALLOWED_CAPABILITIES,
    CapabilityDeniedError,
    CapabilityGrant,
    SandboxSpec,
    SandboxResult,
    SandboxRunner,
)


# ---------------------------------------------------------------------------
# Constants / catalogue
# ---------------------------------------------------------------------------

def test_allowed_capabilities_exact_set():
    assert ALLOWED_CAPABILITIES == frozenset({"CHOWN", "NET_BIND_SERVICE"})


# ---------------------------------------------------------------------------
# CapabilityGrant
# ---------------------------------------------------------------------------

def test_capability_grant_accepts_allowed_names():
    g1 = CapabilityGrant(name="CHOWN")
    g2 = CapabilityGrant(name="NET_BIND_SERVICE")
    assert g1.name == "CHOWN"
    assert g2.name == "NET_BIND_SERVICE"


def test_capability_grant_rejects_disallowed_name():
    with pytest.raises(CapabilityDeniedError):
        CapabilityGrant(name="SYS_ADMIN")


def test_capability_grant_is_frozen():
    g = CapabilityGrant(name="CHOWN")
    with pytest.raises(Exception):
        g.name = "NET_BIND_SERVICE"


# ---------------------------------------------------------------------------
# SandboxSpec validation
# ---------------------------------------------------------------------------

def test_sandbox_spec_defaults():
    spec = SandboxSpec(image="myimg:latest", command=("echo", "hi"))
    assert spec.workdir == "/work"
    assert spec.memory_mb == 512
    assert spec.cpus == 1.0
    assert spec.pids_limit == 256
    assert spec.tmpfs_mb == 256
    assert spec.timeout_s == 120
    assert spec.grants == ()
    assert spec.network is False
    assert spec.use_gvisor is True


@pytest.mark.parametrize("field,value", [
    ("memory_mb", 0),
    ("memory_mb", -1),
    ("pids_limit", 0),
    ("pids_limit", -5),
    ("tmpfs_mb", 0),
    ("tmpfs_mb", -10),
    ("timeout_s", 0),
    ("timeout_s", -1),
    ("cpus", 0),
    ("cpus", -1.0),
])
def test_sandbox_spec_rejects_non_positive_numeric_fields(field, value):
    kwargs = dict(image="img", command=("cmd",))
    kwargs[field] = value
    with pytest.raises(ValueError):
        SandboxSpec(**kwargs)


def test_sandbox_spec_rejects_empty_command():
    with pytest.raises(ValueError):
        SandboxSpec(image="img", command=())


def test_sandbox_spec_is_frozen():
    spec = SandboxSpec(image="img", command=("cmd",))
    with pytest.raises(Exception):
        spec.image = "other"


# ---------------------------------------------------------------------------
# build_argv — exact literal contract
# ---------------------------------------------------------------------------

def test_build_argv_minimal_defaults():
    spec = SandboxSpec(image="alpine:3.19", command=("echo", "hello"))
    runner = SandboxRunner(runtime="podman")
    argv = runner.build_argv(spec)
    expected = [
        "podman", "run", "--rm", "--pull=never",
        "--runtime", "runsc",
        "--network", "none",
        "--user", "1000:1000",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "256",
        "--memory", "512m",
        "--cpus", "1.0",
        "--read-only",
        "--tmpfs", "/work:rw,size=256m",
        "--workdir", "/work",
        "alpine:3.19", "echo", "hello",
    ]
    assert argv == expected


def test_build_argv_with_grants_in_order():
    spec = SandboxSpec(
        image="alpine:3.19",
        command=("echo", "hi"),
        grants=(CapabilityGrant(name="CHOWN"), CapabilityGrant(name="NET_BIND_SERVICE")),
    )
    runner = SandboxRunner()
    argv = runner.build_argv(spec)
    # capability adds must appear, in grant order, before --security-opt
    idx_cap1 = argv.index("--cap-add")
    assert argv[idx_cap1:idx_cap1 + 2] == ["--cap-add", "CHOWN"]
    idx_cap2 = argv.index("--cap-add", idx_cap1 + 1)
    assert argv[idx_cap2:idx_cap2 + 2] == ["--cap-add", "NET_BIND_SERVICE"]
    idx_sec = argv.index("--security-opt")
    assert idx_cap2 < idx_sec


def test_build_argv_network_true_uses_slirp4netns():
    spec = SandboxSpec(image="img", command=("cmd",), network=True)
    runner = SandboxRunner()
    argv = runner.build_argv(spec)
    idx = argv.index("--network")
    assert argv[idx + 1] == "slirp4netns"


def test_build_argv_use_gvisor_false_omits_runtime_flag():
    spec = SandboxSpec(image="img", command=("cmd",), use_gvisor=False)
    runner = SandboxRunner()
    argv = runner.build_argv(spec)
    assert "--runtime" not in argv
    assert "runsc" not in argv


def test_build_argv_custom_runtime_binary():
    spec = SandboxSpec(image="img", command=("cmd",))
    runner = SandboxRunner(runtime="docker")
    argv = runner.build_argv(spec)
    assert argv[0] == "docker"


def test_build_argv_custom_resources_and_workdir():
    spec = SandboxSpec(
        image="img",
        command=("cmd", "arg1"),
        workdir="/opt/task",
        memory_mb=1024,
        cpus=2.5,
        pids_limit=64,
        tmpfs_mb=128,
    )
    runner = SandboxRunner()
    argv = runner.build_argv(spec)
    assert "--memory" in argv and argv[argv.index("--memory") + 1] == "1024m"
    assert "--cpus" in argv and argv[argv.index("--cpus") + 1] == "2.5"
    assert "--pids-limit" in argv and argv[argv.index("--pids-limit") + 1] == "64"
    assert "--tmpfs" in argv and argv[argv.index("--tmpfs") + 1] == "/opt/task:rw,size=128m"
    assert "--workdir" in argv and argv[argv.index("--workdir") + 1] == "/opt/task"
    assert argv[-2:] == ["cmd", "arg1"]


def test_build_argv_never_contains_shell_true_token():
    # sanity: argv is a flat list of strings, "shell=True" concept doesn't
    # apply here, but ensure no such literal token leaks in.
    spec = SandboxSpec(image="img", command=("cmd",))
    runner = SandboxRunner()
    argv = runner.build_argv(spec)
    assert all(isinstance(tok, str) for tok in argv)
    assert "shell=True" not in argv


# ---------------------------------------------------------------------------
# run() — executor injection, success path
# ---------------------------------------------------------------------------

class _FakeCompletedProcess:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_success_invokes_executor_with_exact_kwargs_and_maps_result():
    captured = {}

    def fake_executor(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeCompletedProcess(returncode=0, stdout="out-data", stderr="")

    spec = SandboxSpec(image="alpine", command=("echo", "hi"), timeout_s=30)
    runner = SandboxRunner(runtime="podman", executor=fake_executor)
    result = runner.run(spec)

    assert isinstance(result, SandboxResult)
    assert result.exit_code == 0
    assert result.stdout == "out-data"
    assert result.stderr == ""
    assert result.timed_out is False

    # exact argv passed through
    assert captured["argv"] == runner.build_argv(spec)

    kwargs = captured["kwargs"]
    assert kwargs["timeout"] == 30
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["env"] == os.environ.copy()
    assert "shell" not in kwargs or kwargs["shell"] is not True


def test_run_nonzero_exit_code_propagated():
    def fake_executor(argv, **kwargs):
        return _FakeCompletedProcess(returncode=17, stdout="", stderr="boom")

    spec = SandboxSpec(image="alpine", command=("false",))
    runner = SandboxRunner(executor=fake_executor)
    result = runner.run(spec)
    assert result.exit_code == 17
    assert result.stderr == "boom"
    assert result.timed_out is False


# ---------------------------------------------------------------------------
# run() — timeout path (§21.2 kill-at-limit -> TIMEOUT, never raised)
# ---------------------------------------------------------------------------

def test_run_timeout_maps_to_sandbox_result_exit_124():
    def fake_executor(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    spec = SandboxSpec(image="alpine", command=("sleep", "999"), timeout_s=5)
    runner = SandboxRunner(executor=fake_executor)
    result = runner.run(spec)

    assert isinstance(result, SandboxResult)
    assert result.exit_code == 124
    assert result.timed_out is True


def test_run_timeout_with_partial_bytes_output_is_decoded():
    def fake_executor(argv, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=argv, timeout=kwargs.get("timeout"),
            output=b"partial-out", stderr=b"partial-err",
        )

    spec = SandboxSpec(image="alpine", command=("sleep", "999"))
    runner = SandboxRunner(executor=fake_executor)
    result = runner.run(spec)

    assert result.timed_out is True
    assert result.exit_code == 124
    assert result.stdout == "partial-out"
    assert result.stderr == "partial-err"


def test_run_timeout_with_no_partial_output_yields_empty_strings():
    def fake_executor(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    spec = SandboxSpec(image="alpine", command=("sleep", "999"))
    runner = SandboxRunner(executor=fake_executor)
    result = runner.run(spec)

    assert result.stdout == ""
    assert result.stderr == ""


def test_run_never_uses_shell_true():
    captured = {}

    def fake_executor(argv, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeCompletedProcess(returncode=0, stdout="", stderr="")

    spec = SandboxSpec(image="alpine", command=("echo", "x"))
    runner = SandboxRunner(executor=fake_executor)
    runner.run(spec)
    assert captured["kwargs"].get("shell") is not True


# ---------------------------------------------------------------------------
# Default executor wiring (no real process spawned in this test)
# ---------------------------------------------------------------------------

def test_default_executor_is_subprocess_run():
    runner = SandboxRunner()
    assert runner.executor is subprocess.run
