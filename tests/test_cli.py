from importlib.metadata import version

from typer.testing import CliRunner

import equitypulse
from equitypulse.cli import app

runner = CliRunner()


def test_package_version_matches_installed_metadata() -> None:
    assert equitypulse.__version__ == version("equitypulse")


def test_version_flag_prints_version_and_exits_cleanly() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"equitypulse {equitypulse.__version__}"


def test_no_arguments_shows_help_without_crashing() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output
