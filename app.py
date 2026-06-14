#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PriScanner Web — webowy port desktopowego PriScannera (PySide6 -> Streamlit).

Cały silnik (rozpoznawanie ceny/nazwy, Omnibus, Ceneo, porównywarka Amazon UE,
historia, import/eksport) pochodzi 1:1 z pricemon_core. Tutaj jest tylko warstwa
UI: ciemny motyw „Nuke”, tabela produktów z kolorami oraz panel akcji/szczegółów
zastępujący desktopowe menu kontekstowe i kafelki najechania.

Uruchomienie lokalnie:   streamlit run app.py
"""
import json
import html as _html
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st
import pandas as pd
from st_aggrid import AgGrid, JsCode

import pricemon_core as core
from pricemon_core import (
    Product, resolve_product, compare_amazon_marketplaces,
    find_ceneo_product_url, fmt_money, fmt_dt, change_text, domain,
    has_cheaper_alt, best_cheaper_alt, alts_html, history_html,
    ceneo_offers_html, clean_query, portal_search_url, is_eu_amazon,
    now_iso, data_path, PRICE_PORTALS, APP_NAME, APP_VERSION, _CURL_OK,
)

st.set_page_config(page_title=f"{APP_NAME} {APP_VERSION}", page_icon="🏷️",
                   layout="wide", initial_sidebar_state="collapsed")

# ── kolory zgodne z desktopem ───────────────────────────────────────────────
CLR_UP = "#e0705f"; CLR_DOWN = "#5fb56f"; CLR_NEUTRAL = "#9a9a9a"
CLR_ERROR = "#c98b80"; CLR_UNAVAIL = "#c2a05a"; CLR_TEXT = "#cfcfcf"
CLR_FAV = "#d9a441"; CLR_LINK = "#7db0d0"

# ── motyw „Nuke” (CSS) ───────────────────────────────────────────────────────
st.markdown("""
<style>
:root { color-scheme: dark; }
.stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
    background: #383838; color: #cfcfcf;
}
[data-testid="stHeader"] { background: #333333; border-bottom: 1px solid #1f1f1f; }
.block-container { padding-top: 1.2rem; padding-bottom: 2rem; max-width: 100%;
    padding-left: 2.5rem; padding-right: 2.5rem; }
* { font-family: 'Helvetica Neue','Segoe UI',Arial,sans-serif; }

h1,h2,h3,h4 { color: #e6e6e6 !important; letter-spacing: .5px; }
.pm-title { font-size: 20px; font-weight: 700; color: #e6e6e6; letter-spacing: 1px; }
.pm-sub   { color: #8a8a8a; font-size: 12px; }

/* pola tekstowe / liczbowe */
.stTextInput input, .stNumberInput input, .stSelectbox div[data-baseweb="select"] > div {
    background: #2b2b2b !important; border: 1px solid #1f1f1f !important;
    border-radius: 3px !important; color: #e6e6e6 !important;
}
.stTextInput input:focus, .stNumberInput input:focus { border: 1px solid #d6862a !important; }
label, .stMarkdown p { color: #cfcfcf; }

/* przyciski */
.stButton > button, .stDownloadButton > button {
    background: #4a4a4a; border: 1px solid #202020; border-radius: 3px;
    color: #e6e6e6; padding: 5px 12px; font-size: 13px;
}
.stButton > button:hover, .stDownloadButton > button:hover { background: #565656; border-color:#202020; color:#fff; }
.stButton > button:active { background: #2f2f2f; }
/* przycisk akcentu (type=primary) -> bursztyn */
.stButton > button[kind="primary"], .stButton > button[data-testid="baseButton-primary"] {
    background: #b9742a; border-color: #6e4517; color: #1c130a; font-weight: 600;
}
.stButton > button[kind="primary"]:hover { background: #cf8531; color:#1c130a; }

/* tabela produktów — pełna szerokość, stałe proporcje kolumn */
.pm-table { width: 100%; border-collapse: collapse; border: 1px solid #1f1f1f;
    border-radius: 3px; overflow: hidden; font-size: 12.5px; table-layout: fixed; }
.pm-table th { background: #444444; color: #bdbdbd; padding: 8px 10px; text-align: left;
    border-right: 1px solid #2a2a2a; border-bottom: 1px solid #202020; font-weight: 600;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pm-table th.pm-num { text-align: right; }
.pm-table td { border-bottom: 1px solid #262626; padding: 0; vertical-align: middle; }
.pm-table tr:nth-child(even) td { background: #333333; }
.pm-table tr:nth-child(odd) td  { background: #2e2e2e; }
.pm-table tr:hover td { background: #3a4654; }
/* cały wiersz klikalny: link wypełnia komórkę */
.pm-rowlink { display: block; padding: 7px 10px; color: inherit; text-decoration: none;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer; }
.pm-rowlink:hover, .pm-rowlink:focus { color: inherit; text-decoration: none; }
.pm-num .pm-rowlink { text-align: right; font-variant-numeric: tabular-nums; }
.pm-name .pm-rowlink { white-space: normal; word-break: break-word; }
.pm-fav { color: #d9a441; }
.pm-row-sel td { background: #463a24 !important; box-shadow: inset 3px 0 0 #b9742a; }
.pm-row-sel:hover td { background: #50421f !important; }

/* karty szczegółów (odpowiednik kafelków hover) */
.pm-card { background: #2b2b2b; border: 1px solid #1f1f1f; border-radius: 3px;
    padding: 10px 12px; color: #cfcfcf; font-size: 12.5px; line-height: 1.55; }
.pm-card a { color: #7db0d0; text-decoration: none; }
.pm-card b { color: #e6e6e6; }

hr { border-color: #1f1f1f; }
[data-testid="stExpander"] { border: 1px solid #1f1f1f; border-radius: 3px; background:#2e2e2e; }
.stAlert { background:#2b2b2b; border:1px solid #1f1f1f; }
[data-testid="stToolbar"] { display: none; }
#MainMenu, footer { visibility: hidden; }
</style>
""", unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════════════════════
#  Persystencja (format pliku identyczny z desktopem -> pełna zgodność eksportu)
# ════════════════════════════════════════════════════════════════════════════
def load_products():
    path = data_path()
    products, interval = [], 4
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            interval = int(data.get("interval_hours", 4))
            for d in data.get("products", []):
                if d.get("url"):
                    products.append(Product.from_dict(d))
        except Exception:
            pass
    return products, interval


def save_products():
    try:
        payload = {
            "interval_hours": st.session_state.interval_hours,
            "products": [p.to_dict() for p in st.session_state.products],
        }
        data_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception as e:
        st.session_state.status = f"Nie udało się zapisać danych: {e}"


def find_product(url):
    for i, p in enumerate(st.session_state.products):
        if p.url == url:
            return i, p
    return -1, None


def normalize_url(raw):
    url = (raw or "").strip()
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return url


def set_table_selection(idx):
    """Ustawia wybrany produkt po indeksie — utrzymuje spójność wyboru po
    dodaniu/usunięciu/przesunięciu/imporcie."""
    prods = st.session_state.get("products", [])
    if idx is not None and 0 <= idx < len(prods):
        st.session_state.selected_url = prods[idx].url
    elif prods:
        st.session_state.selected_url = prods[0].url
    else:
        st.session_state.selected_url = None


# ── stan sesji ───────────────────────────────────────────────────────────────
if "products" not in st.session_state:
    prods, interval = load_products()
    st.session_state.products = prods
    st.session_state.interval_hours = interval
    st.session_state.status = ("Gotowe." if prods
                               else "Wklej link do produktu i naciśnij «Dodaj».")
    st.session_state.selected_url = prods[0].url if prods else None
    st.session_state.do_compare = True
    set_table_selection(0 if prods else None)


# ════════════════════════════════════════════════════════════════════════════
#  Operacje na danych (fetch / compare / ceneo) — silnik z pricemon_core
# ════════════════════════════════════════════════════════════════════════════
def apply_fetch_result(p, result):
    """Nanosi wynik resolve_product na produkt (logika z desktopowego on_done)."""
    if result.get("ended"):
        p.status = "ended"; p.error = "Sprzedaż zakończona — oferta wygasła"
        if result.get("name"):
            p.name = result["name"]
        p.current_price = None; p.last_checked = now_iso()
        return "ended"
    if result.get("price") is None and result.get("unavailable"):
        p.status = "unavailable"; p.error = "Produkt chwilowo niedostępny — brak ceny"
        if result.get("name"):
            p.name = result["name"]
        p.current_price = None; p.initial_price = None; p.last_checked = now_iso()
        return "unavailable"
    if result.get("price") is None:
        p.status = "error"; p.error = result.get("_error") or "Nie rozpoznano ceny na stronie"
        p.last_checked = now_iso()
        return "error"

    p.status = "ok"; p.error = ""
    if result.get("name"):
        p.name = result["name"]
    if result.get("currency"):
        p.currency = result["currency"]
    if result.get("image"):
        p.image_url = result["image"]
    if result.get("omnibus") is not None:
        p.omnibus_price = result["omnibus"]
    if result.get("offers"):
        p.shop_offers = result["offers"]

    price = result["price"]
    stamp = now_iso()
    if price is not None:
        last_p = p.history[-1]["p"] if p.history else None
        if last_p is None or abs(last_p - price) >= 0.005:
            p.history.append({"t": stamp, "p": price})
    if p.initial_price is None:
        p.initial_price = price
        if not p.date_added:
            p.date_added = stamp
    p.current_price = price
    p.last_checked = stamp
    return "ok"


def _fetch_one(url, force_headed=False):
    try:
        return resolve_product(url, force_headed=force_headed)
    except core.requests.Timeout:
        return {"price": None, "_error": "Przekroczono czas oczekiwania"}
    except core.requests.HTTPError as e:
        code = e.response.status_code if getattr(e, "response", None) is not None else 0
        if code in (403, 429, 503):
            msg = f"Sklep blokuje automatyczne pobieranie (HTTP {code})"
        else:
            msg = f"Błąd HTTP {code}" if code else f"Błąd sieci: {e}"
        return {"price": None, "_error": msg}
    except core.requests.RequestException as e:
        return {"price": None, "_error": f"Błąd sieci: {e}"}
    except Exception as e:
        return {"price": None, "_error": f"Błąd: {e}"}


def _render_error_message(result):
    if result.get("_error"):
        return result["_error"]
    rerr = result.get("render_error")
    if result.get("blocked"):
        return ("Sklep zablokował dostęp (ochrona antybotowa). Na darmowym "
                "hostingu IP serwerowni jest często blokowane — patrz README.")
    if rerr == "browser-missing":
        return "Brak przeglądarki dla Playwrighta (playwright install chromium)."
    if rerr == "playwright-missing":
        return "Strona ładuje cenę przez JavaScript — wymaga Playwrighta (opcjonalny)."
    if rerr == "timeout":
        return "Render przekroczył czas — możliwa blokada lub CAPTCHA."
    if result.get("http_blocked"):
        return "Sklep blokuje automatyczne pobieranie."
    return "Nie rozpoznano ceny na stronie."


def check_urls(urls, force_headed=False):
    """Sprawdza listę URL-i równolegle, z paskiem postępu. Zwraca liczbę OK."""
    if not urls:
        return 0
    progress = st.progress(0.0, text=f"Sprawdzam 0/{len(urls)}…")
    results = {}
    done = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(_fetch_one, u, force_headed): u for u in urls}
        for fut in as_completed(futs):
            u = futs[fut]
            results[u] = fut.result()
            done += 1
            progress.progress(done / len(urls), text=f"Sprawdzam {done}/{len(urls)}…")
    ok = 0
    eu_to_compare = []
    for u, res in results.items():
        idx, p = find_product(u)
        if not p:
            continue
        if res.get("_error") and res.get("price") is None and not res.get("unavailable") \
                and not res.get("ended"):
            res["_error"] = _render_error_message(res)
        state = apply_fetch_result(p, res)
        if state == "ok":
            ok += 1
            if st.session_state.do_compare and is_eu_amazon(p.url):
                eu_to_compare.append(p)
    progress.empty()

    # porównanie Amazon UE (best-effort) — osobna faza, żeby nie blokować cen
    if eu_to_compare:
        cprog = st.progress(0.0, text="Porównuję z Amazon UE…")
        cdone = 0
        with ThreadPoolExecutor(max_workers=2) as ex:
            cfuts = {ex.submit(compare_amazon_marketplaces, p.url, p.currency,
                               p.current_price): p for p in eu_to_compare}
            for fut in as_completed(cfuts):
                p = cfuts[fut]
                try:
                    p.alts = fut.result() or []
                    p.alts_checked = now_iso()
                except Exception:
                    pass
                cdone += 1
                cprog.progress(cdone / len(eu_to_compare),
                               text=f"Porównuję z Amazon UE… {cdone}/{len(eu_to_compare)}")
        cprog.empty()
    save_products()
    return ok


# ════════════════════════════════════════════════════════════════════════════
#  Pasek narzędzi (góra) — odpowiednik desktopowego top-bar
# ════════════════════════════════════════════════════════════════════════════
st.markdown(
    f"<span class='pm-title'>🏷️ {APP_NAME}</span> "
    f"<span class='pm-sub'>v{APP_VERSION} · monitor cen · "
    f"{'curl_cffi: ✓' if _CURL_OK else 'curl_cffi: ✗ (zainstaluj, by omijać DataDome)'}"
    f"</span>",
    unsafe_allow_html=True)

c1, c2, c3, c4, c5 = st.columns([6, 1.1, 1.3, 1.6, 1.6])
with c1:
    url_in = st.text_input("Link do produktu", key="url_input",
                           placeholder="Wklej link do produktu…",
                           label_visibility="collapsed")
with c2:
    add_clicked = st.button("Dodaj", type="primary", width='stretch')
with c3:
    interval = st.number_input("Co [h]", min_value=1, max_value=168,
                               value=int(st.session_state.interval_hours),
                               step=1, label_visibility="collapsed")
    if interval != st.session_state.interval_hours:
        st.session_state.interval_hours = int(interval)
        save_products()
with c4:
    check_clicked = st.button("Sprawdź wszystko", width='stretch')
with c5:
    st.session_state.do_compare = st.checkbox("Porównuj Amazon UE",
                                              value=st.session_state.do_compare,
                                              help="Pobiera ceny tego samego ASIN-u "
                                                   "z innych rynków Amazon (wolniejsze).")

# ── dodawanie produktu ────────────────────────────────────────────────────────
if add_clicked:
    nurl = normalize_url(url_in)
    if not nurl:
        st.session_state.status = "To nie wygląda na poprawny adres URL."
    else:
        idx, existing = find_product(nurl)
        if existing:
            st.session_state.status = "Ten produkt już jest na liście."
            st.session_state.selected_url = nurl
            set_table_selection(idx)
        else:
            p = Product(url=nurl, name=domain(nurl), date_added=now_iso(),
                        status="fetching")
            st.session_state.products.append(p)
            st.session_state.selected_url = nurl
            set_table_selection(len(st.session_state.products) - 1)
            with st.spinner(f"Pobieram: {domain(nurl)}…"):
                res = _fetch_one(nurl)
                if res.get("price") is None and not res.get("unavailable") \
                        and not res.get("ended"):
                    res["_error"] = _render_error_message(res)
                apply_fetch_result(p, res)
                if st.session_state.do_compare and is_eu_amazon(p.url) and p.current_price:
                    try:
                        p.alts = compare_amazon_marketplaces(p.url, p.currency,
                                                             p.current_price) or []
                        p.alts_checked = now_iso()
                    except Exception:
                        pass
            save_products()
            st.session_state.status = f"Dodano: {p.name or domain(nurl)}"
    st.rerun()

if check_clicked:
    urls = [p.url for p in st.session_state.products]
    if not urls:
        st.session_state.status = "Brak produktów do sprawdzenia."
    else:
        ok = check_urls(urls)
        st.session_state.status = (f"Gotowe — sprawdzono {datetime.now():%H:%M} "
                                   f"({ok} z ceną).")
    st.rerun()


# ════════════════════════════════════════════════════════════════════════════
#  Tabela produktów — AgGrid: klik w wiersz zaznacza (bez checkboxa, miękki
#  rerun), pełna kontrola kolorów komórek i szerokości kolumn. Bez „30 dni”.
# ════════════════════════════════════════════════════════════════════════════
_NUKE_GRID_CSS = {
    ".ag-root-wrapper": {"border": "1px solid #1f1f1f", "border-radius": "3px"},
    ".ag-header": {"background-color": "#444 !important", "border-bottom": "1px solid #202020"},
    ".ag-header-cell-label": {"color": "#bdbdbd !important", "font-weight": "600"},
    ".ag-row": {"font-size": "12.5px", "border-bottom": "1px solid #262626"},
    ".ag-row-even": {"background-color": "#333333 !important"},
    ".ag-row-odd": {"background-color": "#2e2e2e !important"},
    ".ag-row-hover": {"background-color": "#3a4654 !important"},
}

# style komórek: kolor brany z ukrytych pól wiersza (cP/cC/cZ/cT)
_JS_P = JsCode("function(p){return {color:p.data.cP,'line-height':'1.35'}}")
_JS_C = JsCode("function(p){return {color:p.data.cC}}")
_JS_Z = JsCode("function(p){return {color:p.data.cZ}}")
_JS_T = JsCode("function(p){return {color:p.data.cT}}")
_JS_N = JsCode("function(p){return {color:'#9a9a9a'}}")
# cena: domyślny kolor; zielona ★ tylko jako znacznik „taniej na innym Amazonie”
_JS_PRICE = JsCode(
    "function(p){var v=(p.value==null?'':p.value);"
    "return p.data&&p.data._cheap?"
    "'<span style=\"color:#5fb56f\">\u2605</span> '+v:v;}")
_JS_ROWSTYLE = JsCode(
    "function(p){if(p.data&&p.data._sel){return {'background-color':'#463a24',"
    "'box-shadow':'inset 3px 0 0 #b9742a'}}return null;}")


def build_grid_df(products, selected_url):
    rows = []
    for i, p in enumerate(products):
        base = p.name or domain(p.url) or p.url
        if p.status == "error":
            name, cP = "⚠ " + base, CLR_ERROR
        elif p.status in ("unavailable", "ended"):
            name, cP = "⊘ " + base, CLR_UNAVAIL
        else:
            name, cP = base, CLR_TEXT
        if p.favorite:
            name = "★ " + name
        if p.status == "ended":
            price, cC = "zakończona", CLR_UNAVAIL
        elif p.status == "unavailable":
            price, cC = "niedostępny", CLR_UNAVAIL
        elif p.current_price is not None:
            price, cC = fmt_money(p.current_price, p.currency), CLR_TEXT
        elif p.status == "fetching":
            price, cC = "…", CLR_TEXT
        else:
            price, cC = "—", CLR_TEXT
        base_disp = fmt_money(p.initial_price, p.currency) if p.initial_price is not None else "—"
        if p.status in ("unavailable", "ended"):
            chg, cZ = "—", CLR_NEUTRAL
        else:
            chg, cZ = change_text(p), CLR_NEUTRAL
            if p.initial_price and p.current_price is not None:
                diff = p.current_price - p.initial_price
                cZ = CLR_UP if diff > 0.005 else CLR_DOWN if diff < -0.005 else CLR_NEUTRAL
        best = best_cheaper_alt(p)
        best_disp = fmt_money(best["converted"], p.currency) if best else "—"
        cT = CLR_DOWN if best else CLR_NEUTRAL
        checked = "sprawdzam…" if p.status == "fetching" else fmt_dt(p.last_checked)
        rows.append({"Produkt": name, "Cena": price, "Baza": base_disp,
                     "Zmiana": chg, "Taniej": best_disp, "Źródło": domain(p.url),
                     "Dodano": fmt_dt(p.date_added), "Sprawdzono": checked,
                     "_idx": i, "cP": cP, "cC": cC, "cZ": cZ, "cT": cT,
                     "_cheap": has_cheaper_alt(p), "_sel": (p.url == selected_url)})
    cols = ["Produkt", "Cena", "Baza", "Zmiana", "Taniej", "Źródło", "Dodano",
            "Sprawdzono", "_idx", "cP", "cC", "cZ", "cT", "_cheap", "_sel"]
    return pd.DataFrame(rows, columns=cols)


def render_grid(products):
    if not products:
        st.markdown("<div class='pm-card' style='text-align:center;color:#8a8a8a'>"
                    "Lista jest pusta — dodaj pierwszy produkt powyżej.</div>",
                    unsafe_allow_html=True)
        return
    df = build_grid_df(products, st.session_state.get("selected_url"))
    hidden = [{"field": f, "hide": True} for f in ("_idx", "cP", "cC", "cZ", "cT", "_cheap", "_sel")]
    grid_options = {
        "columnDefs": [
            {"field": "Produkt", "flex": 2, "minWidth": 240, "cellStyle": _JS_P,
             "wrapText": True, "autoHeight": True, "tooltipField": "Produkt"},
            {"field": "Cena", "width": 105, "type": "rightAligned", "cellStyle": _JS_C,
             "cellRenderer": _JS_PRICE},
            {"field": "Baza", "width": 100, "type": "rightAligned", "cellStyle": _JS_N},
            {"field": "Zmiana", "width": 135, "type": "rightAligned", "cellStyle": _JS_Z},
            {"field": "Taniej", "width": 105, "type": "rightAligned", "cellStyle": _JS_T},
            {"field": "Źródło", "width": 105, "cellStyle": _JS_N},
            {"field": "Dodano", "width": 120, "cellStyle": _JS_N},
            {"field": "Sprawdzono", "width": 120, "cellStyle": _JS_N},
            *hidden,
        ],
        "rowSelection": "single",
        "suppressRowClickSelection": False,
        "suppressCellFocus": True,
        "getRowStyle": _JS_ROWSTYLE,
        "headerHeight": 36,
    }
    height = min(1200, 90 + 34 * len(products))
    grid = AgGrid(
        df, gridOptions=grid_options, height=height, theme="streamlit",
        allow_unsafe_jscode=True, custom_css=_NUKE_GRID_CSS,
        show_search=False, show_download_button=False, key="pm_grid")

    sel = grid.selected_rows
    idx = None
    if isinstance(sel, pd.DataFrame):
        if not sel.empty:
            idx = int(sel.iloc[0]["_idx"])
    elif sel:
        idx = int(sel[0]["_idx"])
    if idx is not None and 0 <= idx < len(products):
        st.session_state.selected_url = products[idx].url


render_grid(st.session_state.products)

st.markdown(f"<div class='pm-sub' style='margin-top:6px'>{_html.escape(st.session_state.status)}</div>",
            unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════════════════════
#  Panel szczegółów + akcje (zastępuje menu kontekstowe i kafelki najechania)
# ════════════════════════════════════════════════════════════════════════════
st.divider()

if st.session_state.products:
    cur_url = st.session_state.get("selected_url")
    ridx, p = find_product(cur_url)
    if p is None:
        ridx, p = 0, st.session_state.products[0]
        st.session_state.selected_url = p.url

    st.caption("Szczegóły wybranego produktu — kliknij wiersz w tabeli powyżej, aby zmienić.")

    if p:
        left, right = st.columns([1.4, 1])

        # ── lewa kolumna: karty (porównanie / historia / oferty Ceneo / obraz) ──
        with left:
            a_html = alts_html(p, links=True)
            if a_html:
                st.markdown(f"<div class='pm-card'>{a_html}</div>", unsafe_allow_html=True)
            h_html = history_html(p)
            if h_html:
                st.markdown(f"<div class='pm-card' style='margin-top:8px'>{h_html}</div>",
                            unsafe_allow_html=True)
            o_html = ceneo_offers_html(p)
            if o_html:
                st.markdown(f"<div class='pm-card' style='margin-top:8px'>{o_html}</div>",
                            unsafe_allow_html=True)
            if p.omnibus_price is not None:
                st.markdown(f"<div class='pm-card' style='margin-top:8px'>"
                            f"<b>Najniższa cena z 30 dni (Omnibus)</b><br>"
                            f"{fmt_money(p.omnibus_price, p.currency)}</div>",
                            unsafe_allow_html=True)
            if p.image_url:
                st.markdown("<div class='pm-sub' style='margin-top:10px'>Zdjęcie produktu</div>",
                            unsafe_allow_html=True)
                st.image(p.image_url, width=220)
            if not any([a_html, h_html, o_html, p.image_url, p.omnibus_price]):
                st.markdown("<div class='pm-sub'>Brak dodatkowych danych dla tej "
                            "pozycji (sprawdź ją, aby pobrać cenę i szczegóły).</div>",
                            unsafe_allow_html=True)

        # ── prawa kolumna: akcje ────────────────────────────────────────────────
        with right:
            st.markdown(f"<div class='pm-card'><b>{_html.escape(p.name or domain(p.url))}</b>"
                        f"<br><a href='{p.url}' target='_blank'>{_html.escape(domain(p.url))} ↗</a>"
                        f"</div>", unsafe_allow_html=True)

            b1, b2 = st.columns(2)
            with b1:
                if st.button("🔄 Sprawdź ponownie", width='stretch', key="b_check"):
                    with st.spinner("Sprawdzam…"):
                        check_urls([p.url])
                    st.session_state.status = f"Sprawdzono: {p.name or domain(p.url)}"
                    st.rerun()
            with b2:
                if st.button("🌐 Renderuj (Playwright)", width='stretch',
                             key="b_headed", help="Wymusza render przeglądarką "
                             "(jeśli dostępna). Pomaga przy stronach z ceną w JS."):
                    with st.spinner("Renderuję…"):
                        check_urls([p.url], force_headed=True)
                    st.rerun()

            # cena bazowa
            cur_base = p.initial_price if p.initial_price is not None else (p.current_price or 0.0)
            nb = st.number_input("Cena bazowa (odniesienia)", min_value=0.0,
                                 value=float(cur_base or 0.0), step=0.01, format="%.2f",
                                 key=f"base_{p.url}")
            bb1, bb2 = st.columns(2)
            with bb1:
                if st.button("Ustaw bazę", width='stretch', key="b_setbase"):
                    if nb > 0:
                        p.initial_price = nb; save_products()
                        st.session_state.status = "Zaktualizowano cenę bazową."
                        st.rerun()
            with bb2:
                if st.button("Zeruj do bieżącej", width='stretch', key="b_resetbase",
                             disabled=p.current_price is None):
                    p.initial_price = p.current_price; save_products()
                    st.session_state.status = "Cena bazowa = bieżąca."
                    st.rerun()

            t1, t2 = st.columns(2)
            with t1:
                fav_label = "☆ Usuń z ulubionych" if p.favorite else "★ Do ulubionych"
                if st.button(fav_label, width='stretch', key="b_fav"):
                    p.favorite = not p.favorite; save_products(); st.rerun()
            with t2:
                if p.status == "ended":
                    if st.button("▶ Wznów monitorowanie", width='stretch', key="b_resume"):
                        p.status = "new"; p.error = ""; save_products()
                        with st.spinner("Sprawdzam…"):
                            check_urls([p.url])
                        st.rerun()
                else:
                    if st.button("⊘ Oznacz zakończoną", width='stretch', key="b_end"):
                        p.status = "ended"; p.error = "Sprzedaż zakończona — oznaczono ręcznie"
                        p.current_price = None; p.last_checked = now_iso()
                        save_products(); st.rerun()

            # zmiana kolejności (odpowiednik przeciągania wierszy)
            m1, m2 = st.columns(2)
            with m1:
                if st.button("↑ W górę", width='stretch', key="b_up",
                             disabled=ridx == 0):
                    ps = st.session_state.products
                    ps[ridx-1], ps[ridx] = ps[ridx], ps[ridx-1]
                    set_table_selection(ridx - 1)
                    save_products(); st.rerun()
            with m2:
                if st.button("↓ W dół", width='stretch', key="b_down",
                             disabled=ridx >= len(st.session_state.products)-1):
                    ps = st.session_state.products
                    ps[ridx+1], ps[ridx] = ps[ridx], ps[ridx+1]
                    set_table_selection(ridx + 1)
                    save_products(); st.rerun()

            # Ceneo + porównywarki
            if p.name and "ceneo." not in (urlparse(p.url).netloc or "").lower():
                if st.button("🔍 Utwórz monitorowanie Ceneo", width='stretch',
                             key="b_ceneo"):
                    with st.spinner(f"Szukam na Ceneo: {clean_query(p.name)}…"):
                        ref = p.current_price if p.current_price is not None else p.initial_price
                        curl, reason = find_ceneo_product_url(p.name, ref)
                    if curl:
                        idx2, exist = find_product(curl)
                        if exist:
                            st.session_state.status = "Ten produkt Ceneo jest już na liście."
                            st.session_state.selected_url = curl
                            set_table_selection(idx2)
                        else:
                            np = Product(url=curl, name=domain(curl),
                                         date_added=now_iso(), status="fetching")
                            st.session_state.products.insert(ridx + 1, np)
                            st.session_state.selected_url = curl
                            set_table_selection(ridx + 1)
                            save_products()
                            with st.spinner("Pobieram cenę z Ceneo…"):
                                check_urls([curl])
                            st.session_state.status = "Dodano monitorowanie Ceneo."
                        st.rerun()
                    else:
                        purl = portal_search_url("ceneo", p.name)
                        st.session_state.status = ("Nie znalazłem produktu na Ceneo "
                                                   f"({reason}).")
                        if purl:
                            st.markdown(f"<a class='pm-card' style='display:inline-block' "
                                        f"href='{purl}' target='_blank'>Otwórz wyszukiwarkę Ceneo ↗</a>",
                                        unsafe_allow_html=True)

            if p.name:
                links = []
                for key, plabel in PRICE_PORTALS:
                    u = portal_search_url(key, p.name)
                    if u:
                        links.append(f"<a href='{u}' target='_blank'>{plabel} ↗</a>")
                if links:
                    st.markdown("<div class='pm-card'><b>Szukaj w porównywarce</b><br>"
                                + " &nbsp;·&nbsp; ".join(links) + "</div>",
                                unsafe_allow_html=True)

            st.markdown("<div class='pm-sub'>Kopiuj link:</div>", unsafe_allow_html=True)
            st.code(p.url, language=None)

            if st.button("🗑 Usuń z listy", width='stretch', key="b_del"):
                name = p.name or domain(p.url)
                st.session_state.products.pop(ridx)
                st.session_state.selected_url = (
                    st.session_state.products[0].url if st.session_state.products else None)
                set_table_selection(0 if st.session_state.products else None)
                save_products()
                st.session_state.status = f"Usunięto: {name}"
                st.rerun()


# ════════════════════════════════════════════════════════════════════════════
#  Import / eksport (format JSON zgodny z desktopem)
# ════════════════════════════════════════════════════════════════════════════
st.divider()
with st.expander("Import / eksport listy (JSON — zgodny z wersją desktopową)"):
    ie1, ie2 = st.columns(2)
    with ie1:
        st.markdown("**Eksport**")
        export_payload = {
            "format": "pricemon-list", "version": 1, "exported_at": now_iso(),
            "interval_hours": st.session_state.interval_hours,
            "products": [p.to_dict() for p in st.session_state.products],
        }
        st.download_button(
            "⬇ Pobierz listę (.json)",
            data=json.dumps(export_payload, ensure_ascii=False, indent=2),
            file_name=f"pricemon-export-{datetime.now():%Y%m%d}.json",
            mime="application/json", width='stretch',
            disabled=not st.session_state.products)
    with ie2:
        st.markdown("**Import**")
        up = st.file_uploader("Wczytaj plik JSON", type=["json"],
                              label_visibility="collapsed")
        mode = st.radio("Tryb", ["Scal (dodaj nowe)", "Zastąp całość"],
                        horizontal=True, label_visibility="collapsed")
        if up is not None and st.button("Importuj", key="b_import"):
            try:
                data = json.loads(up.read().decode("utf-8"))
            except Exception as e:
                st.session_state.status = f"Nie udało się wczytać pliku: {e}"
                st.rerun()
            raw = data.get("products", []) if isinstance(data, dict) else data
            parsed, seen, invalid = [], set(), 0
            for d in (raw or []):
                if not isinstance(d, dict) or not d.get("url"):
                    invalid += 1; continue
                u = normalize_url(d.get("url"))
                if not u or u in seen:
                    invalid += 1 if not u else 0; continue
                seen.add(u)
                d = dict(d); d["url"] = u
                prod = Product.from_dict(d)
                if not prod.date_added:
                    prod.date_added = now_iso()
                parsed.append(prod)
            if not parsed:
                st.session_state.status = "Import: brak poprawnych pozycji."
            elif mode.startswith("Zastąp"):
                st.session_state.products = parsed
                st.session_state.selected_url = parsed[0].url
                set_table_selection(0)
                save_products()
                st.session_state.status = f"Zastąpiono listę — {len(parsed)} pozycji."
            else:
                added = skipped = 0
                for prod in parsed:
                    if find_product(prod.url)[1]:
                        skipped += 1; continue
                    st.session_state.products.append(prod); added += 1
                save_products()
                st.session_state.status = (f"Import: dodano {added}, "
                                           f"pominięto {skipped}.")
            st.rerun()

with st.expander("ℹ️ Uwagi o hostingu i ograniczeniach"):
    st.markdown("""
- **Scraping z chmury**: darmowe hostingi (Streamlit Community Cloud) działają na
  IP serwerowni, które Amazon/Allegro/AliExpress (DataDome) często blokują.
  `curl_cffi` (fingerprint TLS Chrome) pomaga, ale skuteczność zależy od IP hostingu.
  Dla pełnej niezawodności użyj **VPS-a** (np. swojego, z `PRICEMON_DATA_DIR` na wolumenie).
- **Trwałość danych**: na Community Cloud system plików jest efemeryczny — lista
  znika po restarcie. Używaj **eksportu/importu JSON** jako kopii zapasowej
  (format 1:1 z aplikacją desktopową).
- **Monitoring 24/7**: Streamlit nie uruchamia zadań w tle, gdy nikt nie patrzy.
  Sprawdzanie jest **na żądanie** („Sprawdź wszystko”). Do prawdziwego cyklicznego
  monitoringu uruchom na VPS-ie `cron`, który wywoła skrypt sprawdzający (patrz README).
- **AliExpress / strony z ceną w JS** wymagają Playwrighta — opcjonalny, instrukcja w README.
""")
