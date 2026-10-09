"""Tests for log.py — timestamped output + GA formatting."""

import re


def test_info(capsys):
    from flow_deploy.log import info

    info("test message")
    out = capsys.readouterr().out
    assert re.match(r"\[\d{2}:\d{2}:\d{2}\] test message\n", out)


def test_header(capsys):
    from flow_deploy.log import header

    header("deploy")
    out = capsys.readouterr().out
    assert "── deploy " in out
    assert "─" in out


def test_footer(capsys):
    from flow_deploy.log import footer

    footer("complete")
    out = capsys.readouterr().out
    assert "── complete " in out


def test_step(capsys):
    from flow_deploy.log import step

    step("pulling image...")
    out = capsys.readouterr().out
    assert "  pulling image..." in out


def test_success(capsys):
    from flow_deploy.log import success

    success("web deployed")
    out = capsys.readouterr().out
    assert "✓ web deployed" in out


def test_failure(capsys):
    from flow_deploy.log import failure

    failure("health check timeout")
    out = capsys.readouterr().out
    assert "✗ health check timeout" in out


def test_github_actions_header(capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    from flow_deploy.log import header

    header("deploy")
    out = capsys.readouterr().out
    assert "::group::deploy" in out


def test_github_actions_footer(capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    from flow_deploy.log import footer

    footer("done")
    out = capsys.readouterr().out
    assert "::endgroup::" in out


def test_github_actions_failure(capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    from flow_deploy.log import failure

    failure("deploy failed")
    out = capsys.readouterr().out
    assert "::error::deploy failed" in out


def test_service_start(capsys):
    from flow_deploy.log import service_start

    service_start("web")
    out = capsys.readouterr().out
    assert "▸ web" in out


def test_service_start_github_actions(capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    from flow_deploy.log import service_start

    service_start("web")
    out = capsys.readouterr().out
    assert "::group::web" in out


def test_service_end_github_actions(capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    from flow_deploy.log import service_end

    service_end()
    out = capsys.readouterr().out
    assert "::endgroup::" in out


def test_error(capsys):
    from flow_deploy.log import error

    error("something broke")
    err = capsys.readouterr().err
    assert "ERROR: something broke" in err


def test_logging_survives_a_closed_stream(monkeypatch):
    """A dropped ssh session closes stdout/stderr; logging must not raise."""
    import io

    from flow_deploy import log

    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr("sys.stdout", closed)
    monkeypatch.setattr("sys.stderr", closed)
    log.info("still here")
    log.error("still here")


def test_logging_survives_an_io_error(monkeypatch):
    from flow_deploy import log

    class Hungup:
        def write(self, _text):
            raise OSError(5, "Input/output error")

        def flush(self):
            pass

    monkeypatch.setattr("sys.stdout", Hungup())
    log.step("still here")


def test_error_github_actions_annotates(capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    from flow_deploy.log import error

    error("boom")
    captured = capsys.readouterr()
    assert "::error::boom" in captured.out
    assert "ERROR: boom" in captured.err
