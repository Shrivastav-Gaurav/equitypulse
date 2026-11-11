"""Settings tests. Never read the real .env: every test runs in an empty temp dir with
the relevant environment variables removed, and points Settings at its own env file."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from equitypulse.config import ConfigError, Settings, load_settings

FAKE_TOKEN = "tt-fake-token-1234567890"
FAKE_OPENROUTER = "sk-or-fake-abcdef"
FAKE_LANGSMITH = "ls-fake-xyz"
FAKE_GMAIL_PW = "gmail-app-pw-fake"

ENV_NAMES = [
    "DATABASE_URL",
    "TAPETIDE_MCP_URL",
    "TAPETIDE_API_TOKEN",
    "TAPETIDE_DAILY_BUDGET",
    "OPENROUTER_API_KEY",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
    "GMAIL_USER",
    "GMAIL_APP_PASSWORD",
    "ALERT_TO",
    "NSE_REQUEST_DELAY_SECONDS",
]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    # A stray Settings() with the default env_file must not find the project's real .env.
    monkeypatch.chdir(tmp_path)


def write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "test.env"
    path.write_text(body, encoding="utf-8")
    return path


def full_env(tmp_path: Path) -> Path:
    return write_env(
        tmp_path,
        f"TAPETIDE_API_TOKEN={FAKE_TOKEN}\n"
        "TAPETIDE_DAILY_BUDGET=30\n"
        f"OPENROUTER_API_KEY={FAKE_OPENROUTER}\n"
        f"LANGSMITH_API_KEY={FAKE_LANGSMITH}\n"
        "GMAIL_USER=me@example.com\n"
        f"GMAIL_APP_PASSWORD={FAKE_GMAIL_PW}\n"
        "ALERT_TO=me@example.com\n"
        "NSE_REQUEST_DELAY_SECONDS=2.5\n",
    )


def test_reads_values_from_env_file(tmp_path: Path) -> None:
    s = load_settings(env_file=full_env(tmp_path))
    assert s.tapetide_api_token.get_secret_value() == FAKE_TOKEN
    assert s.tapetide_daily_budget == 30
    assert s.nse_request_delay_seconds == 2.5
    assert s.gmail_user == "me@example.com"
    assert s.openrouter_api_key is not None
    assert s.openrouter_api_key.get_secret_value() == FAKE_OPENROUTER


def test_defaults_match_env_example(tmp_path: Path) -> None:
    s = load_settings(env_file=write_env(tmp_path, f"TAPETIDE_API_TOKEN={FAKE_TOKEN}\n"))
    assert s.database_url == "sqlite:///data/private/equitypulse.db"
    assert s.tapetide_mcp_url == "https://mcp.tapetide.com/mcp"
    assert s.tapetide_daily_budget == 45
    assert s.langsmith_project == "equitypulse"
    assert s.nse_request_delay_seconds == 1.5


def test_environment_variable_overrides_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAPETIDE_DAILY_BUDGET", "10")
    s = load_settings(env_file=full_env(tmp_path))
    assert s.tapetide_daily_budget == 10


def test_every_secret_field_is_secretstr() -> None:
    secret_fields = {
        "tapetide_api_token",
        "openrouter_api_key",
        "langsmith_api_key",
        "gmail_app_password",
    }
    for name in secret_fields:
        annotation = str(Settings.model_fields[name].annotation)
        assert "SecretStr" in annotation, name


def test_missing_required_token_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings(env_file=write_env(tmp_path, "TAPETIDE_DAILY_BUDGET=45\n"))
    message = str(exc.value)
    assert "TAPETIDE_API_TOKEN" in message
    assert ".env" in message


def test_blank_required_token_is_treated_as_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="TAPETIDE_API_TOKEN"):
        load_settings(env_file=write_env(tmp_path, "TAPETIDE_API_TOKEN=   \n"))


def test_missing_env_file_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="TAPETIDE_API_TOKEN"):
        load_settings(env_file=tmp_path / "does-not-exist.env")


def test_blank_optional_secrets_become_none(tmp_path: Path) -> None:
    s = load_settings(
        env_file=write_env(
            tmp_path,
            f"TAPETIDE_API_TOKEN={FAKE_TOKEN}\nOPENROUTER_API_KEY=\nGMAIL_APP_PASSWORD=\n",
        )
    )
    assert s.openrouter_api_key is None
    assert s.gmail_app_password is None
    assert s.gmail_user is None


@pytest.mark.parametrize("bad", ["-1", "abc"])
def test_invalid_budget_raises_config_error(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError, match="TAPETIDE_DAILY_BUDGET"):
        load_settings(
            env_file=write_env(
                tmp_path, f"TAPETIDE_API_TOKEN={FAKE_TOKEN}\nTAPETIDE_DAILY_BUDGET={bad}\n"
            )
        )


def test_error_message_never_contains_secret_values(tmp_path: Path) -> None:
    env = write_env(
        tmp_path,
        f"TAPETIDE_API_TOKEN={FAKE_TOKEN}\nOPENROUTER_API_KEY={FAKE_OPENROUTER}\n"
        "TAPETIDE_DAILY_BUDGET=not-a-number\n",
    )
    with pytest.raises(ConfigError) as exc:
        load_settings(env_file=env)
    message = str(exc.value)
    assert FAKE_TOKEN not in message
    assert FAKE_OPENROUTER not in message


def test_repr_str_and_json_hide_secrets(tmp_path: Path) -> None:
    s = load_settings(env_file=full_env(tmp_path))
    rendered = [repr(s), str(s), s.model_dump_json(), str(s.model_dump())]
    for text in rendered:
        for secret in (FAKE_TOKEN, FAKE_OPENROUTER, FAKE_LANGSMITH, FAKE_GMAIL_PW):
            assert secret not in text
    assert "**********" in repr(s)


def test_secrets_are_secretstr_instances(tmp_path: Path) -> None:
    s = load_settings(env_file=full_env(tmp_path))
    for value in (
        s.tapetide_api_token,
        s.openrouter_api_key,
        s.langsmith_api_key,
        s.gmail_app_password,
    ):
        assert isinstance(value, SecretStr)


def test_default_env_file_is_not_the_real_one_in_tests() -> None:
    # Guard for this test module's isolation: cwd is a temp dir with no .env.
    assert not Path(".env").exists()
    with pytest.raises(ConfigError):
        load_settings()
