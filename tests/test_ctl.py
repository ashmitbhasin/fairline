"""Service controller: the foreground `run` mode systemd uses on EC2, and handing start/stop to systemd."""
import pytest

from tape import ctl


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("FAIRLINE_DATA_DIR", str(tmp_path))
    return tmp_path


def test_run_writes_its_pid_and_becomes_the_service(data, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ctl.os, "execv", lambda exe, argv: calls.append((exe, argv)))
    monkeypatch.setattr(ctl.os, "getpid", lambda: 4242)
    ctl.run(["kalshi-live"])
    assert (data / "run" / "kalshi-live.pid").read_text() == "4242"
    exe, argv = calls[0]
    assert argv == [exe, "-m", "tape.recorder_ws", "--league", "nfl", "--days", "6"]
    assert "start kalshi-live" in capsys.readouterr().out


def test_run_takes_exactly_one_service(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ctl", "run"])
    with pytest.raises(SystemExit) as exc:
        ctl.main()
    assert exc.value.code == 2


def test_start_goes_through_systemd_when_the_unit_is_installed(tmp_path, data, monkeypatch):
    unit = tmp_path / "fairline@.service"
    unit.write_text("")
    commands = []
    monkeypatch.setattr(ctl, "SYSTEMD_UNIT", unit)
    monkeypatch.setattr(ctl.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(ctl.subprocess, "run", lambda cmd, **kw: commands.append(cmd) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(ctl.time, "sleep", lambda s: None)
    monkeypatch.setattr(ctl, "stray_pids", lambda module, known: [])
    monkeypatch.setattr("sys.argv", ["ctl", "restart", "board", "board-web"])
    with pytest.raises(SystemExit) as exc:
        ctl.main()
    assert exc.value.code == 0
    assert commands == [["sudo", "systemctl", "restart", "fairline@board", "fairline@board-web"]]


def test_no_caffeinate_off_the_mac():
    assert (ctl.AWAKE == "caffeinate") == (ctl.sys.platform == "darwin")


@pytest.fixture
def launchctl(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(ctl, "LAUNCH_AGENT", tmp_path / "LaunchAgents" / "com.fairline.autostart.plist")
    monkeypatch.setattr(ctl.sys, "platform", "darwin")
    monkeypatch.setattr(ctl.os, "getuid", lambda: 501)
    monkeypatch.setattr(ctl.subprocess, "run", lambda cmd, **kw: commands.append(cmd) or type("R", (), {"returncode": 0})())
    return commands


def test_autostart_on_starts_only_the_laptop_services_at_login(launchctl):
    assert ctl.autostart("on") == 0
    agent = ctl.plistlib.loads(ctl.LAUNCH_AGENT.read_bytes())
    assert agent["ProgramArguments"] == [ctl.PYTHON, "-m", "tape.ctl", "start", "board", "board-web", "pull"]
    assert agent["WorkingDirectory"] == str(ctl.ROOT) and agent["RunAtLoad"] is True
    # Reloaded, so a second `on` picks up a moved repo or venv.
    assert launchctl == [["launchctl", "bootout", "gui/501/com.fairline.autostart"],
                         ["launchctl", "bootstrap", "gui/501", str(ctl.LAUNCH_AGENT)]]


def test_autostart_off_unloads_and_removes_the_agent(launchctl):
    ctl.autostart("on")
    launchctl.clear()
    assert ctl.autostart("off") == 0
    assert not ctl.LAUNCH_AGENT.exists()
    assert launchctl == [["launchctl", "bootout", "gui/501/com.fairline.autostart"]]


def test_autostart_is_mac_only(launchctl, monkeypatch):
    monkeypatch.setattr(ctl.sys, "platform", "linux")
    assert ctl.autostart("on") == 1
    assert launchctl == [] and not ctl.LAUNCH_AGENT.exists()


@pytest.mark.parametrize("argv", [["autostart"], ["autostart", "board"], ["start", "on"]])
def test_autostart_switch_goes_only_with_autostart(argv, monkeypatch):
    monkeypatch.setattr("sys.argv", ["ctl", *argv])
    with pytest.raises(SystemExit) as exc:
        ctl.main()
    assert exc.value.code == 2


def test_laptop_runs_board_and_pull_once_the_server_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(ctl, "REMOTE_RECORDER", tmp_path / "ssh_config")
    assert ctl.default_names() == ["kalshi-live", "kalshi-backup", "odds", "board", "board-web"]
    (tmp_path / "ssh_config").write_text("Host fairline\n")
    assert ctl.default_names() == ["board", "board-web", "pull"]
