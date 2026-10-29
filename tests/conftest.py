import pytest


@pytest.fixture(autouse=True)
def recorders_here(tmp_path_factory, monkeypatch):
    """Every test starts as if the recorders ran on this machine, whatever this checkout's
    infra/ssh_config says, so a test behaves the same on the laptop and in CI. Tests of the EC2 mode
    point REMOTE_RECORDER at a file they create."""
    from tape import ctl

    monkeypatch.setattr(ctl, "REMOTE_RECORDER", tmp_path_factory.mktemp("local") / "ssh_config")
