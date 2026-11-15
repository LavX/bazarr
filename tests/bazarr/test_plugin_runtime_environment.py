# coding=utf-8
"""The environment policy for plugin workers: what is set, what never is."""
import json
import os
import sys
import textwrap

import pytest

from provider_hub.runtime import environment as env_module
from provider_hub.runtime.environment import (
    CA_BUNDLE_VARIABLES,
    TEMP_VARIABLES,
    find_ca_bundle,
    host_timezone,
    worker_environment,
)
from provider_hub.sandbox import (
    Limits,
    PolicyError,
    RuntimeSpec,
    SandboxPolicy,
    SandboxProbe,
    clean_env,
    spawn_worker,
)

DECLARED = SandboxProbe(abi=0, verified=False, reason="test: no Landlock")
BUNDLE = ""  # a granted file in a temp folder, set by the autouse fixture


@pytest.fixture(autouse=True)
def granted_bundle(tmp_path, monkeypatch):
    """A CA bundle the profile grants, wherever this host keeps its own."""
    global BUNDLE
    folder = os.path.realpath(tmp_path / "certs")
    os.makedirs(folder)
    BUNDLE = os.path.join(folder, "ca.crt")
    with open(BUNDLE, "w") as handle:
        handle.write("x")
    monkeypatch.setattr(env_module, "SYSTEM_READ_PATHS", (folder,))
    return BUNDLE

KINDS = ("translator", "subtitle_provider", "reranker", "x1")


def snapshot(kind, tmp_path, **kwargs):
    kwargs.setdefault("host_env", {"TZ": "Europe/Budapest"})
    kwargs.setdefault("ca_bundle", BUNDLE)
    return worker_environment(kind, home=tmp_path / "data", tmpdir=tmp_path / "tmp", **kwargs)


def test_the_environment_is_exactly_the_policy_list_for_each_kind(tmp_path):
    for kind in KINDS:
        assert snapshot(kind, tmp_path) == {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(tmp_path / "data"),
            "TMPDIR": str(tmp_path / "tmp"),
            "TMP": str(tmp_path / "tmp"),
            "TEMP": str(tmp_path / "tmp"),
            "TZ": "Europe/Budapest",
            "SSL_CERT_FILE": BUNDLE,
            "REQUESTS_CA_BUNDLE": BUNDLE,
            "CURL_CA_BUNDLE": BUNDLE,
        }


def test_the_host_environment_is_never_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("BAZARR_TEST_SECRET", "hunter2")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    monkeypatch.setenv("LD_PRELOAD", "/evil.so")
    env = snapshot("translator", tmp_path, host_env=None)
    assert not {"BAZARR_TEST_SECRET", "PYTHONPATH", "LD_PRELOAD"} & set(env)


@pytest.mark.parametrize(
    "host",
    [
        {"HTTP_PROXY": "http://user:pw@proxy:3128", "HTTPS_PROXY": "http://proxy:3128"},
        {"http_proxy": "http://proxy:3128", "https_proxy": "http://proxy:3128", "all_proxy": "socks5://p:1080"},
        {"NO_PROXY": "localhost", "no_proxy": "localhost", "FTP_PROXY": "x"},
    ],
)
def test_no_proxy_key_is_ever_set_whatever_the_host_has(tmp_path, monkeypatch, host):
    for key, value in host.items():
        monkeypatch.setenv(key, value)
    for kind in KINDS:
        for env in (snapshot(kind, tmp_path, host_env=None), snapshot(kind, tmp_path, host_env=host)):
            assert [key for key in env if "proxy" in key.lower()] == []


@pytest.mark.parametrize("key", ["HTTPS_PROXY", "http_proxy", "ALL_PROXY", "no_proxy", "PIP_PROXY", "Proxy"])
def test_a_proxy_variable_in_extra_is_refused(tmp_path, key):
    with pytest.raises(PolicyError):
        snapshot("translator", tmp_path, extra={key: "http://proxy:3128"})
    with pytest.raises(PolicyError):
        clean_env(extra={key: "x"})


@pytest.mark.parametrize("key", ["HTTPS_PROXY", "http_proxy", "ALL_PROXY"])
def test_a_runtime_spec_refuses_a_proxy_variable_however_it_was_built(tmp_path, key):
    policy = SandboxPolicy.for_interpreter(sys.executable, read_write=[tmp_path])
    with pytest.raises(PolicyError):
        RuntimeSpec(interpreter=sys.executable, args=(), cwd=str(tmp_path), env={"PATH": "/bin", key: "x"}, policy=policy)


@pytest.mark.parametrize("key", ["HOME", "TMPDIR", "tz", "SSL_CERT_FILE", "PATH", "LANG", "PYTHONNOUSERSITE"])
def test_extra_cannot_replace_a_name_the_policy_owns(tmp_path, key):
    with pytest.raises(PolicyError):
        snapshot("translator", tmp_path, extra={key: "x"})


def test_extra_adds_new_names(tmp_path):
    assert snapshot("translator", tmp_path, extra={"PLUGIN_MODE": "fast"})["PLUGIN_MODE"] == "fast"


@pytest.mark.parametrize("kind", ["", "Translator", "a", "bad kind", "../x", None, 7])
def test_a_malformed_kind_is_refused(tmp_path, kind):
    with pytest.raises(PolicyError):
        snapshot(kind, tmp_path)


def test_all_temp_variables_point_at_the_temp_folder(tmp_path):
    env = snapshot("translator", tmp_path)
    assert {env[name] for name in TEMP_VARIABLES} == {str(tmp_path / "tmp")}


@pytest.mark.parametrize("value", [None, "", "x" * 65, "Europe/Bud apest", "UTC\nX=1", "UTC\n", "$(id)", "a=b"])
def test_a_missing_or_odd_timezone_is_left_out(tmp_path, value):
    host = {} if value is None else {"TZ": value}
    assert "TZ" not in snapshot("translator", tmp_path, host_env=host)
    assert host_timezone(host) is None


@pytest.mark.parametrize("value", ["UTC", "Europe/Budapest", "America/Argentina/Buenos_Aires", "EST5EDT,M3.2.0,M11.1.0", "<+03>-3"])
def test_a_zone_name_or_posix_rule_is_passed_through(value):
    assert host_timezone({"TZ": value}) == value


def test_the_timezone_defaults_to_the_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert snapshot("translator", tmp_path, host_env=None)["TZ"] == "Asia/Tokyo"


def test_the_ca_bundle_is_offered_only_when_a_file_the_profile_grants_exists(tmp_path, monkeypatch):
    real = tmp_path / "bundle.crt"
    real.write_text("x")
    monkeypatch.setattr(env_module, "SYSTEM_READ_PATHS", ("/nowhere-granted",))
    assert find_ca_bundle([str(real)]) is None, "a path the sandbox does not grant is never offered"
    monkeypatch.setattr(env_module, "SYSTEM_READ_PATHS", (str(tmp_path),))
    assert find_ca_bundle([str(real)]) == str(real)
    assert find_ca_bundle([str(tmp_path / "missing.crt")]) is None
    assert find_ca_bundle([str(tmp_path)]) is None, "a directory is not a bundle"


def test_an_explicit_bundle_must_be_a_readable_granted_file(tmp_path):
    elsewhere = tmp_path / "elsewhere.crt"
    elsewhere.write_text("x")
    for bad in (str(elsewhere), BUNDLE + ".missing", os.path.dirname(BUNDLE)):
        with pytest.raises(PolicyError):
            snapshot("translator", tmp_path, ca_bundle=bad)


def test_an_explicit_bundle_is_judged_by_where_its_name_leads(tmp_path):
    """A name inside a granted folder that is a link to a file outside it, or reaches out through dots."""
    outside = tmp_path / "outside.crt"
    outside.write_text("x")
    link = os.path.join(os.path.dirname(BUNDLE), "link.crt")
    os.symlink(outside, link)
    dots = os.path.join(os.path.dirname(BUNDLE), "..", "outside.crt")
    for bad in (link, dots):
        with pytest.raises(PolicyError):
            snapshot("translator", tmp_path, ca_bundle=bad)
        assert find_ca_bundle([bad]) is None


def test_an_explicit_bundle_that_is_granted_is_used(tmp_path):
    assert snapshot("translator", tmp_path, ca_bundle=BUNDLE)["SSL_CERT_FILE"] == BUNDLE


def test_a_runtime_spec_keeps_a_read_only_copy_of_its_environment(tmp_path):
    policy = SandboxPolicy.for_interpreter(sys.executable, read_write=[tmp_path])
    source = {"PATH": "/bin"}
    spec = RuntimeSpec(interpreter=sys.executable, args=(), cwd=str(tmp_path), env=source, policy=policy)
    source["HTTPS_PROXY"] = "http://proxy:3128"
    assert "HTTPS_PROXY" not in spec.env, "the spec shares the caller's mapping"
    with pytest.raises(TypeError):
        spec.env["HTTPS_PROXY"] = "http://proxy:3128"
    with pytest.raises(AttributeError):
        spec.env.update({"http_proxy": "x"})
    assert dict(spec.env) == {"PATH": "/bin"}


@pytest.mark.parametrize("env", [{"A=B": "x"}, {"A": 1}, {"A": "x\0y"}, {1: "x"}])
def test_a_runtime_spec_refuses_a_malformed_environment(tmp_path, env):
    policy = SandboxPolicy.for_interpreter(sys.executable, read_write=[tmp_path])
    with pytest.raises(PolicyError):
        RuntimeSpec(interpreter=sys.executable, args=(), cwd=str(tmp_path), env=env, policy=policy)


def test_without_a_system_bundle_no_ca_variable_is_set(tmp_path, monkeypatch):
    monkeypatch.setattr(env_module, "find_ca_bundle", lambda candidates=(): None)
    env = worker_environment("translator", home=tmp_path, tmpdir=tmp_path, host_env={})
    assert not set(CA_BUNDLE_VARIABLES) & set(env)


def test_every_default_candidate_is_inside_a_granted_path():
    from provider_hub.sandbox.policy import SYSTEM_READ_PATHS

    for path in env_module.CA_BUNDLE_CANDIDATES:
        assert any(path == base or path.startswith(base.rstrip("/") + "/") for base in SYSTEM_READ_PATHS), path


def test_a_real_worker_sees_the_policy_environment_and_no_proxy(tmp_path, monkeypatch):
    """Through the real spawn path, with proxy variables set in the host."""
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pass@proxy:3128")
    monkeypatch.setenv("http_proxy", "http://proxy:3128")
    monkeypatch.setenv("TZ", "Europe/Budapest")
    data = tmp_path / "data"
    temp = tmp_path / "tmp"
    data.mkdir()
    temp.mkdir()
    policy = SandboxPolicy.for_interpreter(sys.executable, read_write=[data, temp])
    script = textwrap.dedent("import json, os; print(json.dumps(dict(os.environ)))")
    spec = RuntimeSpec(
        interpreter=sys.executable,
        args=("-I", "-c", script),
        cwd=str(data),
        env=worker_environment("translator", home=data, tmpdir=temp, ca_bundle=BUNDLE),
        policy=policy,
        limits=Limits(),
        term_grace=1.0,
    )
    process = spawn_worker(spec, probe=DECLARED)
    try:
        out, _err = process.communicate(timeout=30)
    finally:
        process.stop(0.5)
    seen = json.loads(out)
    assert seen["HOME"] == str(data) and seen["TMPDIR"] == str(temp) and seen["TZ"] == "Europe/Budapest"
    assert seen["SSL_CERT_FILE"] == BUNDLE
    assert [key for key in seen if "proxy" in key.lower()] == []
    assert "PYTHONPATH" not in seen and os.environ.get("HTTPS_PROXY")
