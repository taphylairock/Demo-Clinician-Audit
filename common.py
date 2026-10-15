"""Shared helpers: config loading, secrets, database access.

Nothing in this file prints secrets. Secrets are read from the environment
(.env) only at the moment they are needed.
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

load_dotenv(ROOT / ".env")


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_settings(path: Path | None = None) -> dict:
    return _read_yaml(path or CONFIG_DIR / "settings.yaml")


def load_column_map(path: Path | None = None) -> dict:
    """Returns {table: {"columns": {real: canonical}, "card_id": optional}}."""
    return _read_yaml(path or CONFIG_DIR / "column_map.yaml").get("tables", {}) or {}


def load_people(path: Path | None = None) -> dict:
    return _read_yaml(path or CONFIG_DIR / "people.yaml")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_flag(name: str) -> bool:
    return env(name, "false").lower() in ("1", "true", "yes", "on")


def require_env(*names: str) -> dict[str, str]:
    """Fetch required env vars. Error messages name the variable, never its value."""
    missing = [n for n in names if not env(n)]
    if missing:
        raise SystemExit(
            "Missing required setting(s) in .env: " + ", ".join(missing)
            + "\nCopy .env.example to .env and fill them in."
        )
    return {n: env(n) for n in names}


def db_path(settings: dict | None = None) -> Path:
    settings = settings or load_settings()
    p = Path(settings.get("database_path", "data/clinician_audit.duckdb"))
    return p if p.is_absolute() else ROOT / p


def get_conn(path: str | Path | None = None, settings: dict | None = None,
             read_only: bool = False) -> duckdb.DuckDBPyConnection:
    p = Path(path) if path else db_path(settings)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(p), read_only=read_only)


def table_exists(conn, name: str) -> bool:
    return bool(conn.execute(
        "select count(*) from information_schema.tables where table_name = ?", [name]
    ).fetchone()[0])


def replace_rows(conn, table: str, df: pd.DataFrame, key_col: str, key_val) -> None:
    """Delete rows where key_col == key_val, then insert df (creates table if new)."""
    if table_exists(conn, table):
        conn.execute(f'delete from "{table}" where "{key_col}" = ?', [key_val])
        have = [r[0] for r in conn.execute(f'describe "{table}"').fetchall()]
        for c in df.columns:
            if c not in have:
                conn.execute(f'alter table "{table}" add column "{c}" varchar')
        df = df.reindex(columns=have)
        conn.register("_incoming", df)
        conn.execute(f'insert into "{table}" select * from _incoming')
        conn.unregister("_incoming")
    else:
        conn.register("_incoming", df)
        conn.execute(f'create table "{table}" as select * from _incoming')
        conn.unregister("_incoming")
