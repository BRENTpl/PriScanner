#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
streamlit_app.py — webowy interfejs PriceMon na Streamlicie.

Cała logika rozpoznawania ceny siedzi w scraper.py (port 1:1 z desktopu),
trwałość w storage.py, a pobieranie w tle + scheduler w engine.py (singleton
trzymany przez st.cache_resource). Ten plik to wyłącznie warstwa UI.

Start:
    export PRICEMON_PASSWORD=cokolwiek
    streamlit run streamlit_app.py
"""

import os
import json
import hashlib
from datetime import datetime

import pandas as pd
import streamlit as st

import scraper
from scraper import Product, now_iso, fmt_money, change_text, is_eu_amazon, domain
from engine import Engine

try:
    from streamlit_autorefresh import st_autorefresh
    _HAS_AUTOREFRESH = True
except Exception:
    _HAS_AUTOREFRESH = False

# --------------------------------------------------------------- konfiguracja
DATA_DIR = os.environ.get("PRICEMON_DATA", "./data")
DB_PATH = os.path.join(DATA_DIR, "pricemon.db")
PASSWORD = os.environ.get("PRICEMON_PASSWORD", "")
WORKERS = int(os.environ.get("PRICEMON_WORKERS", "3"))
DEFAULT_INTERVAL = float(os.environ.get("PRICEMON_INTERVAL_HOURS", "4"))

st.set_page_config(page_title="PriceMon", page_icon="₽", layout="wide",
                   initial_sidebar_state="expanded")

# --- kosmetyka: font mono do liczb + drobne dopieszczenie ciemnego motywu ---
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');
.price { font-family:'JetBrains Mono',monospace; font-weight:700; font-size:1.15rem; }
.base  { font-family:'JetBrains Mono',monospace; color:#9a9a9f; }
.chg   { font-family:'JetBrains Mono',monospace; font-size:.9rem; }
.up    { color:#e0705f; } .down { color:#5fb56f; } .flat { color:#9a9a9f; }
.dom   { font-family:'JetBrains Mono',monospace; font-size:.78rem; color:#6e6e74; }
.badge { font-size:.72rem; padding:1px 7px; border-radius:9px; border:1px solid #3a3a40; }
.badge.ok{color:#5fb56f;border-color:#34503a;} .badge.error{color:#e0705f;border-color:#5a322c;}
.badge.fetching{color:#5fa8c7;border-color:#2f4d5a;} .badge.unavailable{color:#c2a05a;border-color:#54482b;}
.badge.new{color:#9a9a9f;}
.title-link a{color:#d7d7da;text-decoration:none;} .title-link a:hover{color:#5fa8c7;}
div[data-testid="stHorizontalBlock"]{align-items:center;}
.block-container{padding-top:2rem;}
</style>
""", unsafe_allow_html=True)


# ------------------------------------------------------------------- silnik
@st.cache_resource
def get_engine():
    eng = Engine(DB_PATH, max_workers=WORKERS, default_interval_hours=DEFAULT_INTERVAL)
    eng.start()
    return eng


@st.cache_data(show_spinner=False, ttl=86400)
def get_image_bytes(url):
    """Pobiera miniaturę po stronie serwera (curl_cffi — omija hotlink/CORS)."""
    if not url:
        return None
    return scraper.fetch_bytes(url)


def key_for(url, prefix=""):
    return prefix + hashlib.md5(url.encode()).hexdigest()[:10]


# ---------------------------------------------------------------- autoryzacja
def gate():
    if not PASSWORD:
        st.sidebar.warning("Brak PRICEMON_PASSWORD — aplikacja działa bez autoryzacji.")
        return True
    if st.session_state.get("authed"):
        return True
    st.title("PriceMon")
    st.caption("Podaj hasło, aby kontynuować.")
    pw = st.text_input("Hasło", type="password")
    if st.button("Zaloguj", type="primary"):
        import secrets
        if secrets.compare_digest(pw, PASSWORD):
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Nieprawidłowe hasło.")
    st.stop()


# -------------------------------------------------------------- renderowanie
def change_html(p: Product) -> str:
    txt = change_text(p)
    cls = "flat"
    if p.initial_price and p.current_price is not None and p.initial_price > 0:
        diff = p.current_price - p.initial_price
        if diff > 0.005:
            cls = "up"
        elif diff < -0.005:
            cls = "down"
    return f'<span class="chg {cls}">{txt}</span>'


def alts_markdown(p: Product) -> str:
    base = p.current_price
    lines = []
    for a in p.alts:
        flag = a.get("flag", "")
        dom = f"amazon.{a.get('tld','')}"
        url = a.get("url", "")
        if a.get("unavailable") and a.get("price") is None:
            lines.append(f"{flag} [{dom}]({url}) — :orange[niedostępny]")
            continue
        price = a.get("price")
        if price is None:
            continue
        native = fmt_money(price, a.get("currency"))
        conv = a.get("converted")
        tag = native
        if conv is not None and base:
            if conv > base * 1.002:
                tag = f":red[{native}]"
            elif conv < base * 0.998:
                tag = f":green[{native}]"
        conv_s = ""
        if conv is not None and (a.get("currency") or "") != (p.currency or ""):
            conv_s = f" _(≈ {fmt_money(conv, p.currency)})_"
        lines.append(f"{flag} [{dom}]({url}) — {tag}{conv_s}")
    return "\n\n".join(lines) if lines else "_Brak danych z innych rynków._"


def render_product(eng: Engine, p: Product):
    fetching = eng.is_fetching(p.url) or p.status == "fetching"
    with st.container(border=True):
        c = st.columns([0.7, 4.3, 2, 1.6, 1.8, 0.7, 0.8])

        # miniatura
        with c[0]:
            img = get_image_bytes(p.image_url) if p.image_url else None
            if img:
                st.image(img, width=46)
            else:
                st.markdown("🖼️")

        # nazwa + domena + status
        with c[1]:
            name = p.name or domain(p.url)
            st.markdown(f'<div class="title-link">[{name}]({p.url})</div>',
                        unsafe_allow_html=True)
            status = "fetching" if fetching else p.status
            label = {"ok": "OK", "error": "błąd", "unavailable": "niedostępny",
                     "fetching": "pobieram…", "new": "nowy"}.get(status, status)
            extra = f" · {p.error}" if (p.status == "error" and p.error) else ""
            star = "⭐ " if p.favorite else ""
            eu = "🇪🇺 " if is_eu_amazon(p.url) else ""
            st.markdown(
                f'<span class="dom">{star}{eu}{domain(p.url)}</span> '
                f'<span class="badge {status}">{label}</span>'
                f'<span class="dom">{extra}</span>',
                unsafe_allow_html=True)

        with c[2]:
            st.markdown(f'<span class="price">{fmt_money(p.current_price, p.currency)}</span>',
                        unsafe_allow_html=True)
        with c[3]:
            st.markdown(f'<span class="base">baza<br>{fmt_money(p.initial_price, p.currency)}</span>',
                        unsafe_allow_html=True)
        with c[4]:
            st.markdown(change_html(p), unsafe_allow_html=True)

        # ulubione
        with c[5]:
            if st.button("★" if p.favorite else "☆", key=key_for(p.url, "fav"),
                         help="Ulubione"):
                p.favorite = not p.favorite
                eng.store.upsert(p)
                st.rerun()

        # akcje
        with c[6]:
            with st.popover("⋯", use_container_width=True):
                if st.button("⟳ Sprawdź ponownie", key=key_for(p.url, "chk"),
                             use_container_width=True):
                    eng.submit_fetch(p.url, also_compare=True)
                    st.toast("Sprawdzam…"); st.rerun()
                if is_eu_amazon(p.url):
                    if st.button("🇪🇺 Porównaj Amazony UE", key=key_for(p.url, "cmp"),
                                 use_container_width=True):
                        eng.submit_compare(p.url)
                        st.toast("Porównuję rynki UE…"); st.rerun()
                st.divider()
                newbase = st.number_input(
                    "Cena bazowa", value=float(p.initial_price or p.current_price or 0.0),
                    step=1.0, key=key_for(p.url, "baseval"))
                bc1, bc2 = st.columns(2)
                if bc1.button("Zapisz bazę", key=key_for(p.url, "baseset"),
                              use_container_width=True):
                    p.initial_price = newbase
                    eng.store.upsert(p); st.rerun()
                if bc2.button("= bieżąca", key=key_for(p.url, "basereset"),
                              use_container_width=True, help="Zeruj bazę do bieżącej ceny"):
                    p.initial_price = p.current_price
                    eng.store.upsert(p); st.rerun()
                st.divider()
                st.link_button("↗ Otwórz w sklepie", p.url, use_container_width=True)
                if p.name:
                    for pt_key, pt_label in scraper.PRICE_PORTALS:
                        u = scraper.portal_search_url(pt_key, p.name)
                        if u:
                            st.link_button(f"🔎 {pt_label}", u, use_container_width=True)
                st.divider()
                if st.button("🗑 Usuń z listy", key=key_for(p.url, "del"),
                             type="secondary", use_container_width=True):
                    eng.store.delete(p.url)
                    st.toast("Usunięto."); st.rerun()

        # szczegóły: wykres historii + ceny z innych Amazonów
        hist = [h for h in (p.history or []) if isinstance(h.get("p"), (int, float))]
        if hist or (is_eu_amazon(p.url) and p.alts):
            with st.expander("Historia ceny / Amazony UE"):
                if len(hist) >= 2:
                    df = pd.DataFrame(hist)
                    df["t"] = pd.to_datetime(df["t"], errors="coerce")
                    df = df.dropna().set_index("t")
                    st.line_chart(df["p"], height=180)
                elif hist:
                    st.caption("Za mało punktów na wykres — pojawi się po kolejnych sprawdzeniach.")
                if is_eu_amazon(p.url):
                    if p.alts:
                        st.markdown(alts_markdown(p))
                        if p.alts_checked:
                            st.caption("Sprawdzono: " + fmt_dt(p.alts_checked))
                    else:
                        st.caption("Kliknij „Porównaj Amazony UE” w menu ⋯, by pobrać ceny z innych rynków.")


def fmt_dt(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m %H:%M")
    except (ValueError, TypeError):
        return iso or "—"


# ------------------------------------------------------------------- sidebar
def sidebar(eng: Engine):
    st.sidebar.title("Price​Mon")
    st.sidebar.caption("Monitor cen produktów")

    with st.sidebar.form("add", clear_on_submit=True):
        url = st.text_input("Link do produktu",
                            placeholder="https://… (Amazon, Allegro, AliExpress, dowolny sklep)")
        if st.form_submit_button("Dodaj produkt", type="primary", use_container_width=True):
            norm = scraper.normalize_url(url)
            if not norm:
                st.toast("Podaj poprawny adres http(s)://", icon="⚠️")
            elif eng.store.exists(norm):
                st.toast("Ten produkt jest już na liście.", icon="ℹ️")
            else:
                eng.store.upsert(Product(url=norm, date_added=now_iso(), status="new"))
                eng.submit_fetch(norm, also_compare=True)
                st.toast("Dodano — pobieram cenę…", icon="✅")

    st.sidebar.divider()

    c1, c2 = st.sidebar.columns(2)
    if c1.button("⟳ Sprawdź wszystkie", use_container_width=True):
        eng.submit_check_all()
        st.toast("Sprawdzam wszystkie…")
    if c2.button("↻ Odśwież widok", use_container_width=True):
        st.rerun()

    interval = st.sidebar.number_input(
        "Sprawdzaj co (godziny)", min_value=0.25, step=0.25,
        value=float(eng.interval_hours()))
    if abs(interval - eng.interval_hours()) > 1e-6:
        eng.set_interval(interval)

    st.sidebar.divider()

    # eksport / import (format zgodny z desktopem)
    products = eng.store.all()
    payload = {
        "format": "pricemon-list", "version": 1, "exported_at": now_iso(),
        "interval_hours": eng.interval_hours(),
        "products": [p.to_dict() for p in products],
    }
    st.sidebar.download_button(
        "↧ Eksport listy (JSON)",
        data=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name=f"pricemon-export-{datetime.now():%Y%m%d}.json",
        mime="application/json", use_container_width=True)

    up = st.sidebar.file_uploader("↥ Import listy (JSON)", type=["json"])
    if up is not None and not st.session_state.get(f"imported_{up.file_id}"):
        try:
            data = json.loads(up.read().decode("utf-8"))
            raw = data.get("products") if isinstance(data, dict) else data
            added = skipped = invalid = 0
            for d in raw or []:
                if not isinstance(d, dict) or not d.get("url"):
                    invalid += 1; continue
                u = scraper.normalize_url(d["url"])
                if not u:
                    invalid += 1; continue
                if eng.store.exists(u):
                    skipped += 1; continue
                d["url"] = u
                p = Product.from_dict(d)
                if not p.date_added:
                    p.date_added = now_iso()
                eng.store.upsert(p)
                if p.current_price is None:
                    eng.submit_fetch(u, also_compare=True)
                added += 1
            st.session_state[f"imported_{up.file_id}"] = True
            st.toast(f"Import: dodano {added}, pominięto {skipped}, odrzucono {invalid}.")
            st.rerun()
        except Exception as e:
            st.sidebar.error(f"Import nieudany: {e}")

    st.sidebar.divider()

    # status schedulera
    last = eng.last_run.strftime("%d.%m %H:%M") if eng.last_run else "—"
    st.sidebar.caption(
        f"⏱ Scheduler: co {eng.interval_hours():g} h · ostatnio: {last}\n\n"
        f"🌐 Playwright: {'tak' if scraper.playwright_available() else 'nie'} · "
        f"produktów: {len(products)}")

    # auto-odświeżanie
    auto = st.sidebar.checkbox("Auto-odświeżanie (5 s)", value=True,
                               help="Odświeża widok, by pokazać postęp pobierania w tle.")
    return auto


# ---------------------------------------------------------------------- main
def main():
    gate()
    eng = get_engine()
    auto = sidebar(eng)

    if auto and _HAS_AUTOREFRESH:
        st_autorefresh(interval=5000, key="poll")

    products = eng.store.all()
    # ulubione na górze, reszta w kolejności z bazy
    products.sort(key=lambda p: (not p.favorite,))

    st.subheader(f"Obserwowane produkty ({len(products)})")
    if not products:
        st.info("Lista jest pusta. Dodaj produkt w panelu po lewej — wklej link, "
                "resztę aplikacja zrobi sama.")
        return

    for p in products:
        render_product(eng, p)


if __name__ == "__main__":
    main()
