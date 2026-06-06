#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
storage.py — trwałość PriceMon na backendzie.

Zastępuje desktopowy zapis do pricemon.json (QStandardPaths). Trzymamy produkty
w SQLite (jedna tabela, kolumny proste + pola złożone jako JSON). Dostęp
serializowany jednym Lockiem — backend i scheduler chodzą wielowątkowo.

Schemat odpowiada dataclass `Product` ze scraper.py (to_dict / from_dict), więc
import/eksport listy są zgodne z formatem desktopu ("pricemon-list").
"""

import json
import sqlite3
import threading
from pathlib import Path

from scraper import Product, now_iso

_LOCK = threading.RLock()

# Pola złożone, które serializujemy do JSON w jednej kolumnie.
_JSON_FIELDS = ("history", "alts")


class Store:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init_db(self):
        with _LOCK, self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    url           TEXT PRIMARY KEY,
                    name          TEXT DEFAULT '',
                    currency      TEXT DEFAULT '',
                    initial_price REAL,
                    current_price REAL,
                    date_added    TEXT DEFAULT '',
                    last_checked  TEXT DEFAULT '',
                    history       TEXT DEFAULT '[]',
                    alts          TEXT DEFAULT '[]',
                    alts_checked  TEXT DEFAULT '',
                    favorite      INTEGER DEFAULT 0,
                    image_url     TEXT DEFAULT '',
                    sort_order    INTEGER DEFAULT 0,
                    status        TEXT DEFAULT 'new',
                    error         TEXT DEFAULT ''
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT
                )
                """
            )
            # migracja starszych baz: dołóż kolumny, jeśli brakuje
            have = {r["name"] for r in c.execute("PRAGMA table_info(products)")}
            for col, ddl in (("status", "TEXT DEFAULT 'new'"),
                             ("error", "TEXT DEFAULT ''")):
                if col not in have:
                    c.execute(f"ALTER TABLE products ADD COLUMN {col} {ddl}")

    # ------------------------------------------------------------------ map --
    @staticmethod
    def _row_to_product(row):
        d = dict(row)
        for f in _JSON_FIELDS:
            try:
                d[f] = json.loads(d.get(f) or "[]")
            except Exception:
                d[f] = []
        d["favorite"] = bool(d.get("favorite"))
        d.pop("sort_order", None)
        p = Product.from_dict(d)
        # status/error nie są częścią to_dict (format eksportu zgodny z desktopem),
        # więc odtwarzamy je z dedykowanych kolumn osobno.
        st = d.get("status")
        if st:
            p.status = st
        p.error = d.get("error", "") or ""
        return p

    @staticmethod
    def _product_to_params(p, sort_order):
        d = p.to_dict()
        return {
            "url": d["url"],
            "name": d["name"],
            "currency": d["currency"],
            "initial_price": d["initial_price"],
            "current_price": d["current_price"],
            "date_added": d["date_added"],
            "last_checked": d["last_checked"],
            "history": json.dumps(d["history"], ensure_ascii=False),
            "alts": json.dumps(d["alts"], ensure_ascii=False),
            "alts_checked": d["alts_checked"],
            "favorite": 1 if d["favorite"] else 0,
            "image_url": d["image_url"],
            "sort_order": sort_order,
            # status/error spoza to_dict — czytamy wprost z obiektu
            "status": getattr(p, "status", "new") or "new",
            "error": getattr(p, "error", "") or "",
        }

    # --------------------------------------------------------------- queries --
    def all(self):
        with _LOCK, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM products ORDER BY sort_order ASC, rowid ASC"
            ).fetchall()
        return [self._row_to_product(r) for r in rows]

    def get(self, url):
        with _LOCK, self._conn() as c:
            row = c.execute(
                "SELECT * FROM products WHERE url = ?", (url,)
            ).fetchone()
        return self._row_to_product(row) if row else None

    def exists(self, url):
        with _LOCK, self._conn() as c:
            r = c.execute(
                "SELECT 1 FROM products WHERE url = ?", (url,)
            ).fetchone()
        return r is not None

    def _next_sort_order(self, c):
        r = c.execute("SELECT COALESCE(MAX(sort_order), 0) AS m FROM products").fetchone()
        return (r["m"] or 0) + 1

    def upsert(self, p):
        """Wstawia lub aktualizuje produkt (po url). Zachowuje istniejący
        sort_order, dla nowego nadaje kolejny."""
        with _LOCK, self._conn() as c:
            existing = c.execute(
                "SELECT sort_order FROM products WHERE url = ?", (p.url,)
            ).fetchone()
            order = existing["sort_order"] if existing else self._next_sort_order(c)
            params = self._product_to_params(p, order)
            cols = ", ".join(params.keys())
            placeholders = ", ".join(f":{k}" for k in params)
            updates = ", ".join(f"{k}=excluded.{k}" for k in params if k != "url")
            c.execute(
                f"INSERT INTO products ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT(url) DO UPDATE SET {updates}",
                params,
            )

    def delete(self, url):
        with _LOCK, self._conn() as c:
            c.execute("DELETE FROM products WHERE url = ?", (url,))

    def reorder(self, urls):
        """Nadaje kolejność wg listy url-i (drag & drop na froncie)."""
        with _LOCK, self._conn() as c:
            for i, url in enumerate(urls):
                c.execute(
                    "UPDATE products SET sort_order = ? WHERE url = ?", (i, url)
                )

    # -------------------------------------------------------------- settings --
    def get_setting(self, key, default=None):
        with _LOCK, self._conn() as c:
            r = c.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return r["value"] if r else default

    def set_setting(self, key, value):
        with _LOCK, self._conn() as c:
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
