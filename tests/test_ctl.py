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


def test_laptop_runs_board_and_pull_once_the_server_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(ctl, "REMOTE_RECORDER", tmp_path / "ssh_config")
    assert ctl.default_names() == ["kalshi-live", "kalshi-backup", "odds", "board", "board-web"]
    (tmp_path / "ssh_config").write_text("Host fairline\n")
    assert ctl.default_names() == ["board", "board-web", "pull"]
