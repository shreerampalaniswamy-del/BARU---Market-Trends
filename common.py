"""Shared helpers: paths, database schema, config loading, secret scrubbing."""
import os
import sqlite3
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "trends.db"
DEFAULT_CONFIG = ROOT / "config.yaml"

# Anything in these environment variables is removed from log lines and stored messages,
# so an API key can never end up in the committed database.
SECRET_ENV = ("YOUTUBE_API_KEY", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "ANTHROPIC_API_KEY")

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    date TEXT NOT NULL,
    source TEXT NOT NULL,
    market TEXT NOT NULL,
    term TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (date, source, market, term, metric)
);
CREATE TABLE IF NOT EXISTS items (
    source TEXT NOT NULL,
    market TEXT NOT NULL,
    term TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    author TEXT,
    published TEXT,
    views REAL,
    engagement REAL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (source, market, term, url)
);
CREATE TABLE IF NOT EXISTS runs (
    run_ts TEXT NOT NULL,
    source TEXT NOT NULL,
    market TEXT,
    term TEXT,
    status TEXT NOT NULL,
    message TEXT
);
"""


def load_config(path=DEFAULT_CONFIG):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def open_db(path=DEFAULT_DB):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def scrub(text):
    text = str(text)
    for name in SECRET_ENV:
        value = os.environ.get(name)
        if value:
            text = text.replace(value, "***")
    return text
