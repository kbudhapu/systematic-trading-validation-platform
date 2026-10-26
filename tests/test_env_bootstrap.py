"""R3: load_env() must put every .env variable into os.environ so os.getenv() reads (NTFY_TOPIC,
SMTP/EMAIL, Supabase, ...) are populated at runtime. The droplet had NO systemd EnvironmentFile and
the collector never called load_config(), so .env was never in os.environ and every os.getenv read
was silently empty -- only dotenv_values(...) paths worked. This tests the exact mechanism load_env
uses, with a tmp .env (never the real one)."""
from __future__ import annotations

import os

from dotenv import load_dotenv


def test_dotenv_var_visible_via_os_getenv_after_load(tmp_path, monkeypatch):
    key = "MBAPPE_TEST_ENV_ONLY_VAR"
    monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"{key}=hello_from_dotenv\n", encoding="utf-8")
    assert os.getenv(key) is None                      # not in the process env
    load_dotenv(env, override=False)                   # the mechanism load_env() uses
    assert os.getenv(key) == "hello_from_dotenv"       # NOW visible via os.getenv (the thing that was broken)


def test_load_env_override_false_lets_process_env_win(tmp_path, monkeypatch):
    key = "MBAPPE_TEST_OVERRIDE_VAR"
    monkeypatch.setenv(key, "process_wins")            # e.g. a real systemd EnvironmentFile value
    env = tmp_path / ".env"
    env.write_text(f"{key}=dotenv_loses\n", encoding="utf-8")
    load_dotenv(env, override=False)
    assert os.getenv(key) == "process_wins"            # override=False: real env / test monkeypatch wins


def test_config_exposes_load_env_and_it_is_safe_to_call():
    from src.config import load_env
    load_env()                                         # idempotent, explicit ROOT/.env path, never raises
    load_env()
