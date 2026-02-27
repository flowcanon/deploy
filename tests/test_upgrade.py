"""Tests for upgrade.py — binary self-upgrade."""

from unittest.mock import MagicMock, patch

import pytest

from flow_deploy import upgrade


def test_detect_libc_glibc():
    """Default detection returns glibc."""
    mock_result = MagicMock(stdout="linux-gnu", stderr="")
    with patch("subprocess.run", return_value=mock_result):
        assert upgrade._detect_libc() == "glibc"


def test_detect_libc_musl():
    """Detects musl from ldd output."""
    mock_result = MagicMock(stdout="/lib/ld-musl-x86_64.so.1", stderr="")
    with patch("subprocess.run", return_value=mock_result):
        assert upgrade._detect_libc() == "musl"


def test_detect_libc_no_ldd():
    """Falls back to glibc when ldd is missing."""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert upgrade._detect_libc() == "glibc"


def test_binary_path_pyinstaller():
    """Uses sys.executable when running as PyInstaller bundle."""
    with patch.object(upgrade.sys, "_MEIPASS", "/tmp/_MEI123", create=True):
        with patch.object(upgrade.sys, "executable", "/usr/local/bin/flow-deploy"):
            assert upgrade._binary_path() == "/usr/local/bin/flow-deploy"


def test_binary_path_pip_install():
    """Uses shutil.which when running from pip install."""
    with patch.object(upgrade.sys, "_MEIPASS", None, create=True):
        with patch("shutil.which", return_value="/home/user/.local/bin/flow-deploy"):
            assert upgrade._binary_path() == "/home/user/.local/bin/flow-deploy"


def test_binary_path_not_found():
    """Raises RuntimeError when binary cannot be found."""
    with patch.object(upgrade.sys, "_MEIPASS", None, create=True):
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="Cannot determine"):
                upgrade._binary_path()


def test_download_curl(tmp_path):
    """Downloads using curl when available."""
    dest = str(tmp_path / "binary")
    with patch("shutil.which", return_value="/usr/bin/curl"):
        with patch("subprocess.run") as mock_run:
            upgrade._download("https://example.com/bin", dest)
            mock_run.assert_called_once_with(
                ["curl", "-fsSL", "-o", dest, "https://example.com/bin"],
                check=True,
                timeout=120,
            )


def test_download_wget(tmp_path):
    """Falls back to wget when curl is missing."""
    dest = str(tmp_path / "binary")
    with patch("shutil.which", side_effect=lambda cmd: "/usr/bin/wget" if cmd == "wget" else None):
        with patch("subprocess.run") as mock_run:
            upgrade._download("https://example.com/bin", dest)
            mock_run.assert_called_once_with(
                ["wget", "-qO", dest, "https://example.com/bin"],
                check=True,
                timeout=120,
            )


def test_download_no_tools():
    """Raises when neither curl nor wget is available."""
    with patch("shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="curl or wget"):
            upgrade._download("https://example.com/bin", "/tmp/bin")


@patch("flow_deploy.upgrade._download")
@patch("flow_deploy.upgrade._binary_path")
@patch("flow_deploy.upgrade._detect_libc")
def test_upgrade_success(mock_libc, mock_path, mock_dl, tmp_path):
    """Full upgrade succeeds: downloads, replaces binary."""
    binary = tmp_path / "flow-deploy"
    binary.write_text("old")
    mock_libc.return_value = "glibc"
    mock_path.return_value = str(binary)
    mock_dl.side_effect = lambda url, dest: open(dest, "w").write("new")

    result = upgrade.upgrade()

    assert result == 0
    mock_dl.assert_called_once()
    assert "latest/download/flow-deploy-linux-glibc" in mock_dl.call_args[0][0]


@patch("flow_deploy.upgrade._download", side_effect=RuntimeError("network error"))
@patch("flow_deploy.upgrade._binary_path")
@patch("flow_deploy.upgrade._detect_libc")
def test_upgrade_download_failure(mock_libc, mock_path, mock_dl, tmp_path):
    """Upgrade returns 1 on download failure and cleans up temp file."""
    binary = tmp_path / "flow-deploy"
    binary.write_text("old")
    mock_libc.return_value = "glibc"
    mock_path.return_value = str(binary)

    result = upgrade.upgrade()

    assert result == 1
    assert binary.read_text() == "old"  # original untouched


@patch("flow_deploy.upgrade._binary_path", side_effect=RuntimeError("not found"))
def test_upgrade_no_binary(mock_path):
    """Upgrade returns 1 when binary path cannot be determined."""
    result = upgrade.upgrade()
    assert result == 1


@patch("flow_deploy.upgrade._download")
@patch("flow_deploy.upgrade._binary_path")
@patch("flow_deploy.upgrade._detect_libc")
def test_upgrade_cli(mock_libc, mock_path, mock_dl, tmp_path):
    """Upgrade command via CLI."""
    from click.testing import CliRunner

    from flow_deploy.cli import main

    binary = tmp_path / "flow-deploy"
    binary.write_text("old")
    mock_libc.return_value = "glibc"
    mock_path.return_value = str(binary)
    mock_dl.side_effect = lambda url, dest: open(dest, "w").write("new")

    runner = CliRunner()
    result = runner.invoke(main, ["upgrade"])
    assert result.exit_code == 0
