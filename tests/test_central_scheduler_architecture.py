from __future__ import annotations

import http.client
import importlib.util
import os
import plistlib
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKERS = {"jobseeker", "connectman", "sendman", "postliker", "commentator"}
SERVICE_DIRS = {
    "jobseeker": "JobSeeker",
    "connectman": "ConnectMan",
    "sendman": "SendMan",
    "postliker": "PostLiker",
    "commentator": "Commentator",
}


def test_all_workers_are_in_central_scheduler_mode_and_keep_exec_ready():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    offenders = {}
    for svc in WORKERS:
        env = compose["services"][svc].get("environment") or {}
        if env.get("CENTRAL_SCHEDULER_MODE") != "1":
            offenders[svc] = env
    assert offenders == {}


def test_central_mode_entrypoints_use_no_action_crontab_not_worker_run_sh():
    offenders = {}
    for svc, dirname in SERVICE_DIRS.items():
        text = (ROOT / "services" / dirname / "entrypoint.sh").read_text()
        health = (ROOT / "services" / dirname / "scripts" / "healthcheck.sh").read_text()
        central = (ROOT / "services" / dirname / "crontab.central").read_text()
        problems = []
        if "CENTRAL_SCHEDULER_MODE" not in text or "crontab.central" not in text:
            problems.append("entrypoint does not select central crontab")
        if "/app/scripts/run.sh" in central or "run_unlocked.sh" in central:
            problems.append("central crontab starts worker")
        if "central scheduler idle" not in central:
            problems.append("central crontab is not readable no-action contract")
        if "CENTRAL_SCHEDULER_MODE" not in health or "crontab.central" not in health:
            problems.append("healthcheck does not validate central crontab")
        if problems:
            offenders[svc] = problems
    assert offenders == {}


def test_dockerfiles_copy_central_crontab_into_each_image():
    offenders = {}
    for svc, dirname in SERVICE_DIRS.items():
        text = (ROOT / "services" / dirname / "Dockerfile").read_text()
        if "COPY crontab.central /app/crontab.central" not in text:
            offenders[svc] = text
    assert offenders == {}


def test_launchd_plist_and_wrappers_are_idempotent_and_do_not_install_on_verify():
    plist_path = ROOT / "launchd" / "ai.linkedin.scheduler.plist"
    wrapper = ROOT / "scripts" / "launchd" / "linkedin_scheduler_launchd.sh"
    manage = ROOT / "scripts" / "launchd" / "linkedin_scheduler_launchd_manage.sh"
    plist = plistlib.loads(plist_path.read_bytes())

    assert plist["Label"] == "ai.linkedin.scheduler"
    assert plist["WorkingDirectory"] == str(ROOT)
    assert plist["EnvironmentVariables"]["TZ"] == "America/Argentina/Buenos_Aires"
    assert plist["ProgramArguments"] == [str(wrapper)]
    assert plist.get("KeepAlive") is True
    assert plist.get("RunAtLoad") is True
    assert "StartInterval" not in plist
    assert "run-due" in wrapper.read_text()
    assert "sleep 60" in wrapper.read_text()
    manage_text = manage.read_text()
    for cmd in ["verify", "install", "load", "unload", "reload", "status", "logs", "uninstall", "kickstart"]:
        assert f'{cmd})' in manage_text
    assert "sudo" not in manage_text
    res = subprocess.run(["bash", str(manage), "verify"], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert res.returncode == 0, res.stdout


def test_launchd_manage_install_and_load_are_idempotent_and_propagate_bootstrap_errors(tmp_path):
    manage = ROOT / "scripts" / "launchd" / "linkedin_scheduler_launchd_manage.sh"
    text = manage.read_text()
    assert "/usr/bin/install -m 644" in text
    assert "launchctl print" in text
    assert "launchctl enable" in text
    assert "launchctl bootstrap" in text
    assert "|| launchctl enable" not in text

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    log = tmp_path / "launchctl.log"
    (fakebin / "launchctl").write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$LAUNCHCTL_LOG\"\n"
        "case \"$1\" in\n"
        "  print) exit 3 ;;\n"
        "  enable) exit 0 ;;\n"
        "  bootstrap) echo bootstrap failed >&2; exit 42 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    (fakebin / "launchctl").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["HOME"] = str(tmp_path / "home")
    env["LAUNCHCTL_LOG"] = str(log)

    res = subprocess.run(["bash", str(manage), "load"], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

    assert res.returncode == 42, res.stdout
    assert (tmp_path / "home" / "Library" / "LaunchAgents" / "ai.linkedin.scheduler.plist").exists()
    uid = os.getuid()
    assert log.read_text().splitlines() == [
        f"print gui/{uid}/ai.linkedin.scheduler",
        f"enable gui/{uid}/ai.linkedin.scheduler",
        f"bootstrap gui/{uid} {tmp_path / 'home' / 'Library' / 'LaunchAgents' / 'ai.linkedin.scheduler.plist'}",
    ]


def load_jobseeker_extractor():
    path = ROOT / "services" / "JobSeeker" / "scripts" / "linkedin_extractor.py"
    spec = importlib.util.spec_from_file_location("linkedin_extractor", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_extractor_guest_url_preserves_easy_apply_and_known_filters():
    mod = load_jobseeker_extractor()
    url = mod.build_guest_url(
        "https://www.linkedin.com/jobs/search/?keywords=Platform%20Engineer&location=Remote&f_AL=true&f_WT=2&f_JT=F&sortBy=R&geoId=92000000&evil=https://example.test",
        30,
    )
    assert "f_AL=true" in url
    assert "f_JT=F" in url
    assert "sortBy=R" in url
    assert "evil=" not in url


def test_extractor_fetch_retries_incomplete_chunked_reads(monkeypatch):
    mod = load_jobseeker_extractor()
    calls = {"count": 0}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            calls["count"] += 1
            if calls["count"] == 1:
                raise http.client.IncompleteRead(b"partial", 10)
            return b"ok"

    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda req, timeout: Response())

    assert mod.fetch("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search", attempts=2) == (200, "ok")
    assert calls["count"] == 2
