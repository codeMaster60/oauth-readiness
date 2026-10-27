"""Guide storage: curated guides ship as JSON in the repo; generated ones go to SQLite."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Optional

from .models import Guide

ROOT = Path(__file__).resolve().parent.parent
CURATED_DIR = ROOT / "data" / "guides"
CATALOG_FILE = ROOT / "data" / "catalog.json"
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data" / "generated.sqlite3"))


class Store:
    def __init__(self, db_path: Path = DB_PATH):
        self.curated: dict[str, Guide] = {}
        for f in sorted(CURATED_DIR.glob("*.json")):
            g = Guide.model_validate_json(f.read_text())
            self.curated[g.slug] = g
        self.catalog: list[dict] = json.loads(CATALOG_FILE.read_text()) if CATALOG_FILE.exists() else []
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS guides (slug TEXT PRIMARY KEY, json TEXT, updated TEXT)")
        self.db.commit()

    def get(self, slug: str) -> Optional[Guide]:
        if slug in self.curated:
            return self.curated[slug]
        row = self.db.execute("SELECT json FROM guides WHERE slug=?", (slug,)).fetchone()
        return Guide.model_validate_json(row[0]) if row else None

    def save(self, g: Guide) -> None:
        if g.slug in self.curated:  # never overwrite a hand-checked guide; keep both
            g.slug = f"{g.slug}-generated"
        self.db.execute("INSERT OR REPLACE INTO guides VALUES (?,?,?)", (g.slug, g.model_dump_json(), g.updated))
        self.db.commit()

    def generated(self) -> list[Guide]:
        rows = self.db.execute("SELECT json FROM guides ORDER BY updated DESC").fetchall()
        return [Guide.model_validate_json(r[0]) for r in rows]

    def find(self, query: str) -> Optional[Guide]:
        """Exact-ish match on name/slug/website so typing 'Gmail' opens the curated guide."""
        q = query.strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")
        if not q:
            return None
        for g in list(self.curated.values()) + self.generated():
            names = {g.slug, g.name.lower(), g.website.lower().removeprefix("https://").removeprefix("www.").rstrip("/")}
            names |= {a.lower() for a in ALIASES.get(g.slug, [])}
            if q in names:
                return g
        return None


ALIASES = {
    "google-gmail": ["gmail", "google", "google workspace", "gmail.com", "mail.google.com", "google mail"],
    "github": ["github.com"],
    "slack": ["slack.com"],
    "notion": ["notion.so", "notion.com"],
    "hubspot": ["hubspot.com"],
    "linkedin": ["linkedin.com"],
}
