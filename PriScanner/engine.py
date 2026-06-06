#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
engine.py — warstwa wykonawcza PriceMon dla Streamlita.

Streamlit przelatuje skrypt od nowa przy każdej interakcji, więc cała logika
stanowa (pula wątków pobierających + scheduler cyklicznych sprawdzeń) żyje tutaj,
w jednym obiekcie `Engine`. Streamlit trzyma go jako singleton przez
`st.cache_resource`, dzięki czemu scheduler chodzi w tle niezależnie od rerunów
i sesji użytkowników.

Logika pobierania (do_fetch / do_compare / check_all) jest przeniesiona z wersji
FastAPI — bez SSE; zamiast emitować zdarzenia, zapisuje stan do SQLite, a UI
odczytuje go przy kolejnym przebiegu (polling / auto-refresh).
"""

import time
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import scraper
from scraper import (
    Product, resolve_product, compare_amazon_marketplaces,
    is_eu_amazon, now_iso,
)
from storage import Store

ALTS_TTL_HOURS = 12


def _error_message(result) -> str:
    rerr = result.get("render_error")
    if result.get("blocked"):
        return ("Sklep zablokował dostęp (ochrona antybotowa). "
                "IP serwera mogło zostać oznaczone.")
    if rerr == "browser-missing":
        return "Brak przeglądarki dla Playwrighta (playwright install chromium)."
    if rerr == "playwright-missing":
        return "Strona ładuje cenę przez JavaScript, a Playwright jest niedostępny."
    if rerr == "timeout":
        return "Render przekroczył czas — możliwa blokada lub CAPTCHA."
    if result.get("rendered"):
        return "Nie udało się odczytać ceny (możliwa blokada lub CAPTCHA)."
    if result.get("http_blocked"):
        return "Sklep blokuje automatyczne pobieranie."
    return "Nie rozpoznano ceny na stronie."


class Engine:
    def __init__(self, db_path, max_workers=3, default_interval_hours=4.0):
        self.store = Store(db_path)
        self.executor = ThreadPoolExecutor(max_workers=max_workers,
                                            thread_name_prefix="fetch")
        self.default_interval = default_interval_hours
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.last_run = None          # kiedy scheduler ostatnio przeleciał
        self.scheduler_started = False
        self._scheduler = threading.Thread(
            target=self._scheduler_loop, name="scheduler", daemon=True)

    # ----------------------------------------------------------- public API --
    def start(self):
        if not self.scheduler_started:
            self.scheduler_started = True
            self._scheduler.start()

    def interval_hours(self) -> float:
        try:
            return max(0.25, float(self.store.get_setting(
                "interval_hours", self.default_interval)))
        except (TypeError, ValueError):
            return self.default_interval

    def set_interval(self, hours: float):
        self.store.set_setting("interval_hours", max(0.25, float(hours)))

    def is_fetching(self, url) -> bool:
        with self._lock:
            return url in self._inflight

    def submit_fetch(self, url, also_compare=True):
        """Zleca pobranie w tle. Stan 'pobieram…' pokazujemy z pamięci
        (is_fetching) — nie utrwalamy go w bazie, by po restarcie serwera nic
        nie utknęło na 'fetching'. W bazie lądują tylko stany końcowe."""
        with self._lock:
            if url in self._inflight:
                return
            self._inflight.add(url)
        self.executor.submit(self._fetch_worker, url, also_compare)

    def submit_check_all(self):
        for p in self.store.all():
            self.submit_fetch(p.url, also_compare=is_eu_amazon(p.url) and self._alts_stale(p))

    def submit_compare(self, url):
        self.executor.submit(self.do_compare, url)

    # ------------------------------------------------------------- workers --
    def _fetch_worker(self, url, also_compare):
        try:
            self.do_fetch(url, also_compare)
        finally:
            with self._lock:
                self._inflight.discard(url)

    def do_fetch(self, url, also_compare=False):
        p = self.store.get(url)
        if p is None:
            return
        try:
            result = resolve_product(url, force_headed=False)
        except scraper.requests.Timeout:
            p.status, p.error = "error", "Przekroczono czas oczekiwania"
            self.store.upsert(p); return
        except scraper.requests.HTTPError as e:
            code = e.response.status_code if getattr(e, "response", None) else 0
            p.status = "error"
            p.error = (f"Sklep blokuje pobieranie (HTTP {code})"
                       if code in (403, 429, 503) else f"Błąd HTTP {code or '—'}")
            self.store.upsert(p); return
        except Exception as e:
            p.status, p.error = "error", f"Błąd: {e}"
            self.store.upsert(p); return

        if result["price"] is None:
            if result.get("unavailable"):
                p.status, p.error = "unavailable", "Produkt niedostępny"
                if result.get("name") and not p.name:
                    p.name = result["name"]
                p.last_checked = now_iso()
                self.store.upsert(p); return
            p.status, p.error = "error", _error_message(result)
            self.store.upsert(p); return

        price = result["price"]
        if result.get("name"):
            p.name = result["name"]
        if result.get("currency"):
            p.currency = result["currency"]
        if result.get("image") and not p.image_url:
            p.image_url = result["image"]
        if p.initial_price is None:
            p.initial_price = price
        if not p.history or abs((p.history[-1].get("p") or 0) - price) > 0.005:
            p.history.append({"t": now_iso(), "p": price})
            p.history = p.history[-1000:]
        p.current_price = price
        p.last_checked = now_iso()
        p.status, p.error = "ok", ""
        self.store.upsert(p)

        if also_compare and is_eu_amazon(url):
            self.do_compare(url)

    def do_compare(self, url):
        p = self.store.get(url)
        if p is None or not is_eu_amazon(url):
            return
        try:
            alts = compare_amazon_marketplaces(url, p.currency)
        except Exception:
            alts = []
        p = self.store.get(url)
        if p is None:
            return
        p.alts = alts
        p.alts_checked = now_iso()
        self.store.upsert(p)

    # ----------------------------------------------------------- scheduler --
    def _alts_stale(self, p: Product) -> bool:
        if not p.alts_checked:
            return True
        try:
            return datetime.fromisoformat(p.alts_checked) < \
                datetime.now() - timedelta(hours=ALTS_TTL_HOURS)
        except ValueError:
            return True

    def _scheduler_loop(self):
        # krótka zwłoka po starcie serwera
        self._stop.wait(3)
        while not self._stop.is_set():
            try:
                self.submit_check_all()
                self.last_run = datetime.now()
            except Exception:
                pass
            # czekamy interwał, ale budzimy się co minutę, by reagować na zmianę
            target = self.interval_hours() * 3600
            waited = 0.0
            while waited < target and not self._stop.is_set():
                self._stop.wait(60)
                waited += 60

    def shutdown(self):
        self._stop.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
