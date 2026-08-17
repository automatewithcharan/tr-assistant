from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv.main import DotEnv

from pkscreener.classes.AssistantLiveRuntime import _runtime_env
from pkscreener.classes.EnvironmentLoader import load_repository_environment


def _isolated_dotenv_loader(calls):
    """Use python-dotenv directly while recording the requested precedence."""

    def load(path, *, override):
        calls.append((Path(path), override))
        return DotEnv(
            dotenv_path=path,
            override=override,
            encoding="utf-8",
        ).set_as_environment_variables()

    return load


@pytest.mark.parametrize(
    ("process_value", "dotenv_value"),
    [("1", "0"), ("0", "1")],
)
def test_explicit_kite_dry_run_value_wins_over_dotenv(
    tmp_path, monkeypatch, process_value, dotenv_value
):
    (tmp_path / ".env.dev").write_text(
        f"KITE_DRY_RUN={dotenv_value}\n", encoding="utf-8"
    )
    monkeypatch.setenv("KITE_DRY_RUN", process_value)
    calls = []

    selected = load_repository_environment(
        tmp_path, loader=_isolated_dotenv_loader(calls)
    )

    assert selected == tmp_path / ".env.dev"
    assert os.environ["KITE_DRY_RUN"] == process_value
    assert calls == [(tmp_path / ".env.dev", False)]


def test_missing_process_variable_loads_from_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env.dev").write_text(
        "CLI_CONFIGURATION_FROM_DOTENV=loaded\n", encoding="utf-8"
    )
    monkeypatch.delenv("CLI_CONFIGURATION_FROM_DOTENV", raising=False)

    load_repository_environment(
        tmp_path, loader=_isolated_dotenv_loader([])
    )

    assert os.environ["CLI_CONFIGURATION_FROM_DOTENV"] == "loaded"


def test_unrelated_explicit_safety_values_are_not_overwritten(tmp_path, monkeypatch):
    explicit = {
        "KITE_EXECUTION_MODE": "paper",
        "KITE_AUTO_TRADE": "0",
        "KITE_AUTO_BUY": "0",
        "KITE_AUTO_SELL": "0",
        "SIGNALMONITOR_AUTOTRADE_ENABLED": "0",
        "SIGNALMONITOR_AUTOTRADE_REAL_ORDERS": "0",
    }
    (tmp_path / ".env.dev").write_text(
        "\n".join(f"{key}=hostile" for key in explicit) + "\n",
        encoding="utf-8",
    )
    for key, value in explicit.items():
        monkeypatch.setenv(key, value)

    load_repository_environment(
        tmp_path, loader=_isolated_dotenv_loader([])
    )

    assert {key: os.environ[key] for key in explicit} == explicit


def test_dotenv_loading_does_not_print_secret_values(tmp_path, monkeypatch, capsys):
    secret_value = "test-secret-must-not-be-printed"
    (tmp_path / ".env.dev").write_text(
        f"DISCORD_BOT_TOKEN={secret_value}\n", encoding="utf-8"
    )
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    load_repository_environment(
        tmp_path, loader=_isolated_dotenv_loader([])
    )

    captured = capsys.readouterr()
    assert secret_value not in captured.out
    assert secret_value not in captured.err


def test_runtime_child_environment_inherits_preserved_process_value(
    tmp_path, monkeypatch
):
    (tmp_path / ".env.dev").write_text("KITE_DRY_RUN=0\n", encoding="utf-8")
    monkeypatch.setenv("KITE_DRY_RUN", "1")

    load_repository_environment(
        tmp_path, loader=_isolated_dotenv_loader([])
    )
    child_environment = _runtime_env(tmp_path)

    assert os.environ["KITE_DRY_RUN"] == "1"
    assert child_environment["KITE_DRY_RUN"] == "1"


def test_normal_cli_fallback_env_file_still_loads_missing_configuration(
    tmp_path, monkeypatch
):
    (tmp_path / ".env").write_text(
        "NORMAL_CLI_CONFIGURATION=available\n", encoding="utf-8"
    )
    monkeypatch.delenv("NORMAL_CLI_CONFIGURATION", raising=False)
    calls = []

    selected = load_repository_environment(
        tmp_path, loader=_isolated_dotenv_loader(calls)
    )

    assert selected == tmp_path / ".env"
    assert os.environ["NORMAL_CLI_CONFIGURATION"] == "available"
    assert calls == [(tmp_path / ".env", False)]
