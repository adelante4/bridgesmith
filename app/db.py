"""SQLite engine + session dependency."""

import os
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, text

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DB_PATH = DATA_DIR / "app.db"

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})

# create_all() only creates missing tables — it never alters an existing
# table's columns. On a pre-existing data/app.db from before these columns
# were added, that leaves the DB schema stale and every insert touching a
# missing column fails at runtime. No migration framework in this prototype
# (spec.md), so patch new columns onto existing tables by hand here.
_NEW_COLUMNS = [
    ("generatedarticle", "pdf_path", "VARCHAR"),
    ("pdfdigest", "tone_signals", "TEXT"),
    ("pdfdigest", "design_notes", "TEXT"),
    ("pdfdigest", "brand_primary_color", "VARCHAR"),
    ("pdfdigest", "brand_accent_color", "VARCHAR"),
    ("pdfdigest", "brand_font_family", "VARCHAR"),
    ("generatedarticle", "sender_pdf_digest_id", "INTEGER"),
    ("generatedarticle", "receiver_pdf_digest_id", "INTEGER"),
]


def _add_missing_columns() -> None:
    with engine.begin() as conn:
        for table, column, col_type in _NEW_COLUMNS:
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
            if existing and column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "assets").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "renders").mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def get_session():
    with Session(engine) as session:
        yield session
