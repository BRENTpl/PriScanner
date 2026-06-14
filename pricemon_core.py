#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pricemon_core — silnik PriScannera (scraping + model danych), bez Qt.

Wyodrębniony 1:1 z desktopowego PriScanner_v1_22.py (część logiczna 1 i 2).
Zmiany względem desktopu:
  * usunięto zależność od Qt (QStandardPaths -> ścieżka z env/HOME),
  * renderowanie Playwrightem przeniesione do osobnego skryptu render_worker.py
    wywoływanego subprocessem (na serwerze i tak działa best-effort).
Wszystkie funkcje rozpoznawania ceny/nazwy/Omnibus/Ceneo/Amazon-UE
pozostają identyczne jak w aplikacji desktopowej.
"""

APP_NAME = "PriScanner"
APP_VERSION = "1.22-web"

import sys
import os
import re
import json
import html
import time
import random
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, urljoin, quote, quote_plus

import requests
from bs4 import BeautifulSoup

# opcjonalnie: curl_cffi podszywa się pod fingerprint TLS/JA3 prawdziwego Chrome
try:
    from curl_cffi import requests as _curl_requests
    _CURL_OK = True
    _CURL_ERR = ""
except Exception as _e:
    _curl_requests = None
    _CURL_OK = False
    _CURL_ERR = f"{type(_e).__name__}: {_e}"


CURRENCY_SYMBOL = {
    "PLN": "zł", "EUR": "€", "USD": "$", "GBP": "£",
    "CHF": "CHF", "CZK": "Kč", "UAH": "₴", "SEK": "kr", "NOK": "kr",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}


def parse_price_string(s):
    """Wyciąga liczbę zmiennoprzecinkową z dowolnego zapisu ceny.

    Obsługuje formaty PL/EU/US: '1 299,00 zł', '1.299,00', '1,299.00',
    '49.99', '1.299', itp. Zwraca float albo None.
    """
    if s is None:
        return None
    s = str(s)
    m = re.search(r"\d[\d\s\u00a0\u202f.,]*\d|\d", s)
    if not m:
        return None
    num = m.group(0)
    num = re.sub(r"[\s\u00a0\u202f]", "", num)

    if "," in num and "." in num:
        # Ostatni separator jest dziesiętny.
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        parts = num.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            num = num.replace(",", ".")          # 1299,00 -> dziesiętny
        else:
            num = num.replace(",", "")           # 1,299 / 1,234,567 -> tysiące
    elif "." in num:
        parts = num.split(".")
        if len(parts) > 2:
            num = num.replace(".", "")           # 1.234.567 -> tysiące
        elif len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3:
            num = num.replace(".", "")           # 1.299 -> prawdopodobnie tysiące
        # w pozostałych przypadkach kropka pozostaje separatorem dziesiętnym
    try:
        return float(num)
    except ValueError:
        return None


def _meta(soup, attrs):
    tag = soup.find("meta", attrs=attrs)
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


META_PRICE_KEYS = [
    {"property": "product:price:amount"},
    {"property": "og:price:amount"},
    {"itemprop": "price"},
    {"name": "twitter:data1"},
]
META_CURRENCY_KEYS = [
    {"property": "product:price:currency"},
    {"property": "og:price:currency"},
    {"itemprop": "priceCurrency"},
]


_SITE_NAME_RE = re.compile(
    r"^(amazon(\.[a-z.]+)?|allegro(\.pl)?|aliexpress(\.com)?|sklep)$", re.I)


def _is_site_name(s):
    """True, gdy tekst to sama nazwa serwisu (np. „Amazon.pl”), a nie produktu."""
    return bool(s) and bool(_SITE_NAME_RE.match(s.strip()))


# końcówki tytułów stron porównywarek/sklepów (np. „… - Ceny i opinie - Ceneo.pl”)
_TITLE_TAIL_RES = [
    re.compile(r"\s*[-–—|]\s*ceny i opinie\s*[-–—|]\s*ceneo\.pl\s*$", re.I),
    re.compile(r"\s*[-–—|]\s*opinie i ceny\s*[-–—|]\s*ceneo\.pl\s*$", re.I),
    re.compile(r"\s*[-–—|]\s*ceny i opinie\s*$", re.I),
    re.compile(r"\s*[-–—|]\s*ceneo\.pl\s*$", re.I),
    re.compile(r"\s*[-–—|]\s*allegro(\.pl)?\s*$", re.I),
    re.compile(r"\s*[-–—|]\s*skąpiec(\.pl)?\s*$", re.I),
]


def _clean_portal_title(s):
    if not s:
        return s
    s = s.strip()
    for rx in _TITLE_TAIL_RES:
        s = rx.sub("", s)
    return s.strip()


def extract_name(soup):
    # Amazon: kanoniczny tytuł produktu
    pt = soup.select_one("#productTitle")
    if pt:
        t = pt.get_text(" ", strip=True)
        if t and not _is_site_name(t):
            return _clean_portal_title(t)
    for attrs in ({"property": "og:title"}, {"name": "twitter:title"}):
        v = _meta(soup, attrs)
        if v and not _is_site_name(v):
            return _clean_portal_title(v)
    if soup.title and soup.title.string:
        t = soup.title.string.strip()
        if t and not _is_site_name(t):
            return _clean_portal_title(t)
    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(" ", strip=True)
        if t and not _is_site_name(t):
            return _clean_portal_title(t)
    return None


def _iter_jsonld(soup):
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string if tag.string is not None else tag.get_text()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, list):
                stack.extend(cur)
            elif isinstance(cur, dict):
                graph = cur.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
                yield cur


def _offer_price(offers):
    items = offers if isinstance(offers, list) else [offers]
    for off in items:
        if not isinstance(off, dict):
            continue
        for key in ("price", "lowPrice", "highPrice"):
            if off.get(key) not in (None, ""):
                p = parse_price_string(off[key])
                if p is not None:
                    return p, off.get("priceCurrency")
        spec = off.get("priceSpecification")
        if spec:
            p, c = _offer_price(spec)
            if p is not None:
                return p, c or off.get("priceCurrency")
    return None, None


def jsonld_product(soup):
    name = price = currency = None
    for obj in _iter_jsonld(soup):
        t = obj.get("@type")
        types = [str(x) for x in (t if isinstance(t, list) else [t])]
        if "Product" in types:
            if not name and obj.get("name"):
                name = str(obj["name"]).strip()
            if price is None and obj.get("offers") is not None:
                p, c = _offer_price(obj["offers"])
                if p is not None:
                    price, currency = p, c
        if price is None and ("Offer" in types or "AggregateOffer" in types):
            p, c = _offer_price(obj)
            if p is not None:
                price, currency = p, c
    return name, price, currency


def infer_currency(text):
    for sym, code in (("zł", "PLN"), ("PLN", "PLN"), ("€", "EUR"), ("EUR", "EUR"),
                      ("£", "GBP"), ("GBP", "GBP"), ("CHF", "CHF"), ("Kč", "CZK"),
                      ("$", "USD"), ("USD", "USD")):
        if sym in text:
            return code
    return ""


# --- frazy oznaczające brak dostępności (PL / EN / DE) ---
UNAVAILABLE_PHRASES = (
    "chwilowo niedostępn", "obecnie niedostępn", "produkt niedostępn",
    "currently unavailable", "temporarily out of stock", "out of stock",
    "derzeit nicht verfügbar", "nicht verfügbar", "non disponibile",
)

# --- regiony strony, które NIE są głównym produktem (rekomendacje itp.) ---
NOISE_SELECTORS = (
    "script", "style", "noscript", "template", "header", "footer", "nav",
    "[id*=carousel i]", "[class*=carousel i]",
    "[id*=recommend i]", "[class*=recommend i]",
    "[id*=similar i]", "[class*=similar i]",
    "[id*=related i]", "[class*=related i]",
    "[id*=sponsor i]", "[class*=sponsor i]",
    "[id*=p13n i]", "[class*=p13n i]",
    "[id*=sims i]", "[class*=sims i]",
    "[id*=also i]", "[id*=cross-sell i]", "[id*=upsell i]",
    "[id*=frequently i]", "[class*=bought-together i]",
    "[data-cel-widget*=sims i]", "[data-cel-widget*=rhf i]",
)


def _strip_noise(soup):
    """Usuwa z drzewa karuzele/rekomendacje/sponsorowane, by skan nie łapał
    cen innych produktów. Uwaga: usuwa też <script>, więc wywoływać dopiero
    po odczycie meta i JSON-LD."""
    for sel in NOISE_SELECTORS:
        try:
            for el in soup.select(sel):
                el.decompose()
        except Exception:
            continue


# --- Amazon: cena WYŁĄCZNIE z buyboxa (nie z karuzel polecanych produktów) ---
AMAZON_PRICE_SELECTORS = (
    "#corePrice_feature_div .a-price .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
    "#corePrice_desktop .a-price .a-offscreen",
    "#price_inside_buybox",
    "#newBuyBoxPrice",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    "#priceblock_saleprice",
    "#apex_desktop .a-price .a-offscreen",
    "#buybox .a-price .a-offscreen",
    "#desktop_buybox .a-price .a-offscreen",
    "#qualifiedBuybox .a-price .a-offscreen",
    "#corePrice_feature_div .a-price-whole",
)
AMAZON_BUYBOX_SELECTORS = (
    "#outOfStock", "#availability", "#buybox", "#desktop_buybox",
    "#qualifiedBuybox", "#rightCol", "#centerCol",
)


def is_amazon(url):
    return "amazon." in (urlparse(url).netloc or "").lower()


def is_aliexpress(url):
    return "aliexpress." in (urlparse(url).netloc or "").lower()


def is_js_required_host(url):
    """Serwisy w 100% renderowane po stronie klienta — bez przeglądarki
    (Playwright) zwykły GET zwraca pustą skorupę bez ceny."""
    return is_aliexpress(url)


def is_strict_host(url):
    """Serwisy, na których globalny skan tekstu jest zawodny (pełno cen innych
    ofert / zahaszowane klasy). Cenę bierzemy tylko ze źródeł pewnych."""
    host = (urlparse(url).netloc or "").lower()
    return any(k in host for k in
               ("amazon.", "allegro.", "allegrolokalnie.", "aliexpress."))


# AliExpress nie ma JSON-LD; dane (w tym cena) siedzą w window.runParams /
# __INIT_DATA__. Pola formatedPrice/formatedActivityPrice to gotowy do
# wyświetlenia napis ceny GŁÓWNEJ oferty (nie rekomendacji).
_ALIEXPRESS_PRICE_KEYS = (
    "formatedActivityPrice", "formatedPrice", "salePriceString",
    "discountPriceString", "skuActivePrice", "actMinPrice",
    "minActivityAmount", "minAmount", "priceText", "formatTradePrice",
)

# selektory ceny w wyrenderowanym DOM-ie AliExpress (klasy bywają zahaszowane,
# ale zwykle zawierają "price"/"Price"/"current")
_ALIEXPRESS_DOM_SELECTORS = (
    ".product-price-value", ".product-price-current",
    "[class*=Price_current i]", "[class*=price--current i]",
    "[class*=currentPriceText i]", "span[class*=uniformBannerBoxPrice i]",
)


def aliexpress_price(html_text, soup=None):
    """(cena, waluta) z osadzonego JSON-a AliExpress lub DOM-u albo (None, None)."""
    for key in _ALIEXPRESS_PRICE_KEYS:
        m = re.search(r'"%s"\s*:\s*"([^"]{1,40})"' % key, html_text)
        if m:
            p = parse_price_string(m.group(1))
            if p is not None:
                return p, infer_currency(m.group(1))
    # wariant obiektowy: "minAmount":{"value":12.34,"currency":"PLN"}
    m = re.search(
        r'"value"\s*:\s*([\d.]+)\s*,\s*"currency(?:Code)?"\s*:\s*"([A-Z]{3})"',
        html_text,
    )
    if m:
        try:
            return float(m.group(1)), m.group(2)
        except ValueError:
            pass
    # DOM (po renderze) — z pominięciem rekomendacji
    if soup is not None:
        for sel in _ALIEXPRESS_DOM_SELECTORS:
            try:
                for el in soup.select(sel):
                    if _in_recommendation(el):
                        continue
                    txt = el.get_text(" ", strip=True)
                    p = parse_price_string(txt)
                    if p is not None:
                        return p, infer_currency(txt)
            except Exception:
                continue
    return None, None


# Allegro (i wiele PL sklepów) wstawia cenę głównej oferty w opis meta:
#   og:description: "Kup teraz ... za 95 zł - w kategorii ..."
#   description:    "... za 95.00PLN - w kategorii ..."
_DESC_PRICE_RE = re.compile(
    r"\bza\s+([\d][\d\s\u00a0.,]*\d|\d)\s*(zł|PLN|€|EUR|\$|USD|£|GBP)",
    re.IGNORECASE,
)


def price_from_description(soup):
    """(cena, waluta) wyłuskane z opisu meta albo (None, None)."""
    for attrs in ({"property": "og:description"},
                  {"name": "description"},
                  {"property": "description"}):
        v = _meta(soup, attrs)
        if not v:
            continue
        m = _DESC_PRICE_RE.search(v)
        if m:
            p = parse_price_string(m.group(1))
            if p is not None:
                return p, infer_currency(m.group(2))
    return None, None


# kontenery rekomendacji/sponsorowanych na Amazonie (przodek ceny)
_REC_ANCESTOR_RE = re.compile(
    r"sims|carousel|recommend|sponsor|p13n|also|cross-sell|upsell|similar|"
    r"rhf|bought|valuepick|comparison|advert",
    re.IGNORECASE,
)


def _in_recommendation(el):
    """True, jeśli element leży wewnątrz bloku rekomendacji/sponsorowanego."""
    node = el.parent
    for _ in range(16):
        if node is None or getattr(node, "name", None) is None:
            break
        marker = " ".join(filter(None, [
            node.get("id", "") or "",
            " ".join(node.get("class", []) or []),
            node.get("data-cel-widget", "") or "",
        ]))
        if marker and _REC_ANCESTOR_RE.search(marker):
            return True
        node = node.parent
    return False


# kolumny GŁÓWNEGO produktu — cena buyboxa leży wewnątrz nich; rekomendacje nie
AMAZON_MAIN_CONTAINERS = (
    "#corePrice_feature_div", "#corePriceDisplay_desktop_feature_div",
    "#corePrice_desktop", "#apex_desktop", "#apex_offerDisplay_desktop",
    "#price", "#buyBoxAccordion", "#buybox", "#desktop_buybox",
    "#qualifiedBuybox", "#rightCol", "#centerCol", "#ppd",
)


def amazon_price(soup):
    """(cena, waluta) z dokładnych kontenerów ceny buyboxa albo (None, None)."""
    for sel in AMAZON_PRICE_SELECTORS:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if not el:
            continue
        txt = el.get("content") or el.get_text(" ", strip=True)
        p = parse_price_string(txt)
        if p is not None:
            return p, infer_currency(txt)
    return None, None


def amazon_price_loose(soup):
    """Fallback: pierwsza cena WEWNĄTRZ kolumny produktu (nie na całej stronie),
    dzięki czemu nie łapie cen z karuzel rekomendacji. Wywoływać tylko, gdy
    produkt NIE jest oznaczony jako niedostępny."""
    for container_sel in AMAZON_MAIN_CONTAINERS:
        try:
            container = soup.select_one(container_sel)
        except Exception:
            continue
        if container is None:
            continue
        # cena w .a-offscreen
        for el in container.select(".a-price .a-offscreen"):
            if _in_recommendation(el):
                continue
            txt = el.get_text(" ", strip=True)
            p = parse_price_string(txt)
            if p is not None:
                return p, infer_currency(txt)
        # cena rozbita whole/fraction
        whole = container.select_one(".a-price-whole")
        if whole is not None and not _in_recommendation(whole):
            txt = whole.get_text(" ", strip=True).rstrip(", ")
            frac = whole.find_next(class_="a-price-fraction")
            if frac:
                txt = f"{txt},{frac.get_text(strip=True)}"
            p = parse_price_string(txt)
            if p is not None:
                ctx = whole.parent.get_text(" ", strip=True) if whole.parent else ""
                return p, infer_currency(ctx)
    return None, None


def is_unavailable(soup, url):
    """True, jeśli GŁÓWNY produkt jest niedostępny (a nie któryś z poleconych)."""
    # 0) zakończona/wygasła oferta (Allegro „Sprzedaż zakończona” itp.)
    try:
        if is_offer_ended(soup.get_text(" ", strip=True)):
            return True
    except Exception:
        pass
    # 1) sygnał ze schema.org (offers.availability)
    for obj in _iter_jsonld(soup):
        offers = obj.get("offers")
        items = offers if isinstance(offers, list) else [offers]
        for off in items:
            if isinstance(off, dict):
                av = str(off.get("availability", "")).lower()
                if any(k in av for k in ("outofstock", "soldout", "discontinued")):
                    return True
    # 2) tekst w obrębie buyboxa / kolumny zakupowej
    selectors = AMAZON_BUYBOX_SELECTORS if is_amazon(url) else (
        "#buybox", "#availability", "[class*=availability i]",
        "[class*=buybox i]", "[class*=add-to-cart i]", "[class*=stock i]",
    )
    for sel in selectors:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el:
            t = el.get_text(" ", strip=True).lower()
            if any(ph in t for ph in UNAVAILABLE_PHRASES):
                return True
    return False


# ----------------------------------------------------------------------------
#  Porównywarka cen między Amazonami z Unii Europejskiej (ten sam ASIN)
# ----------------------------------------------------------------------------
# (tld, waluta, etykieta, flaga). Amazon dzieli ASIN między rynkami.
AMAZON_EU = [
    ("de", "EUR", "Niemcy", "🇩🇪"),
    ("fr", "EUR", "Francja", "🇫🇷"),
    ("it", "EUR", "Włochy", "🇮🇹"),
    ("es", "EUR", "Hiszpania", "🇪🇸"),
    ("nl", "EUR", "Holandia", "🇳🇱"),
    ("pl", "PLN", "Polska", "🇵🇱"),
    ("se", "SEK", "Szwecja", "🇸🇪"),
    ("com.be", "EUR", "Belgia", "🇧🇪"),
]
EU_TLDS = {tld for tld, *_ in AMAZON_EU}
_EU_FLAG = {tld: flag for tld, cur, label, flag in AMAZON_EU}
_EU_CUR = {tld: cur for tld, cur, label, flag in AMAZON_EU}   # se→SEK, com.be→EUR…


# Porównywarki cen — etykiety do menu „Szukaj w porównywarce”.
# Łatwo dopisać/poprawić wpis; URL buduje portal_search_url().
PRICE_PORTALS = [("ceneo", "Ceneo"), ("google", "Google Zakupy"),
                 ("allegro", "Allegro"), ("skapiec", "Skąpiec")]


_QUERY_STOP = {"w", "o", "i", "z", "ze", "do", "na", "dla", "od", "po", "za",
               "oraz", "the", "and", "of", "a", "u", "+", "-"}


def clean_query(name, max_words=8):
    """Sprowadza tytuł do samej nazwy produktu (marka + model). Dosłowne
    wyszukiwarki (np. Skąpiec) gubią się przy całym opisie, więc ucinamy ogon
    „… : Amazon.pl: …", znaczniki sklepów, wszystko po pierwszym separatorze
    (| • – —, myślnik ze spacjami) i po pierwszym przecinku, a na końcu skracamy
    do `max_words` słów i obcinamy końcowe słowa-wypełniacze (np. „w”, „o”)."""
    q = (name or "").strip()
    if not q:
        return ""
    q = re.split(r"\s*:\s*Amazon\b", q, maxsplit=1, flags=re.I)[0]
    q = re.split(r"\s*[-–|]\s*(?:Amazon|Allegro|Ceneo|Sk[aą]piec|AliExpress|"
                 r"Media\s*Expert|x-?kom|Morele|Empik|RTV\s*EURO)\b.*$", q,
                 maxsplit=1, flags=re.I)[0]
    q = re.split(r"\s+[|•–—\-]\s+", q, maxsplit=1)[0]    # opis po separatorze
    q = q.split(",")[0]                                   # człon opisowy po przecinku
    q = re.sub(r"\b(?:Amazon\.\w+|Allegro\.pl|AliExpress)\b", "", q, flags=re.I)
    q = re.sub(r"\s{2,}", " ", q).strip(" -–|:,.")
    words = q.split()[:max_words]
    while words and words[-1].lower().strip(".,") in _QUERY_STOP:
        words.pop()
    return " ".join(words).strip()


def portal_search_url(key, name):
    """Buduje adres wyszukiwania produktu w danej porównywarce."""
    # Skąpiec ma prymitywne, dosłowne wyszukiwanie (AND po słowach) — dostaje
    # tylko kilka pierwszych słów (marka + model). Ceneo/Google są elastyczne.
    q = clean_query(name, max_words=4 if key == "skapiec" else 8)
    if not q:
        return ""
    if key == "ceneo":          # potwierdzony wzorzec: ;szukaj-<fraza>
        return "https://www.ceneo.pl/;szukaj-" + quote_plus(q)
    if key == "allegro":        # elastyczne wyszukiwanie: listing?string=<fraza>
        return "https://allegro.pl/listing?string=" + quote_plus(q)
    if key == "skapiec":        # natywne wyszukiwanie: ?query=<fraza>&categoryId=
        return ("https://www.skapiec.pl/szukaj?query=" + quote_plus(q)
                + "&categoryId=")
    if key == "google":         # Zakupy Google — uniwersalny fallback
        return "https://www.google.com/search?tbm=shop&q=" + quote_plus(q)
    return ""


def amazon_tld(url):
    """Zwraca końcówkę rynku Amazona, np. 'de', 'pl', 'com.be' albo None."""
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("amazon."):
        return host[len("amazon."):]
    return None


def amazon_asin(url):
    """Wyłuskuje 10-znakowy ASIN z adresu produktu Amazona albo None."""
    m = re.search(r"/(?:dp|gp/product|gp/aw/d|product|gp/offer-listing)/([A-Z0-9]{10})", url)
    if m:
        return m.group(1).upper()
    m = re.search(r"[/?&](?:asin|ASIN)=([A-Z0-9]{10})", url)
    return m.group(1).upper() if m else None


def is_eu_amazon(url):
    return is_amazon(url) and amazon_tld(url) in EU_TLDS and bool(amazon_asin(url))


# --- kursy walut (frankfurter.dev/.app, dane ECB) z prostym cache ---
_FX_CACHE = {}            # (frm,to) -> (rate, timestamp)
_FX_LOCK = threading.Lock()


_FX_ENDPOINTS = (
    "https://api.frankfurter.dev/v1/latest?base={frm}&symbols={to}",
    "https://api.frankfurter.app/latest?from={frm}&to={to}",   # zapasowy
)


def fx_rate(frm, to):
    """Kurs przeliczeniowy frm->to (float) albo None. Cache 6 h.
    Próbuje kolejno frankfurter.dev, a potem starszego frankfurter.app."""
    frm = (frm or "").upper()
    to = (to or "").upper()
    if not frm or not to:
        return None
    if frm == to:
        return 1.0
    key = (frm, to)
    now = time.time()
    with _FX_LOCK:
        c = _FX_CACHE.get(key)
        if c and now - c[1] < 6 * 3600:
            return c[0]
    rate = None
    for tmpl in _FX_ENDPOINTS:
        try:
            r = requests.get(tmpl.format(frm=frm, to=to), timeout=10)
            r.raise_for_status()
            rate = r.json().get("rates", {}).get(to)
            rate = float(rate) if rate is not None else None
            if rate is not None:
                break
        except Exception:
            rate = None
    if rate is not None:
        with _FX_LOCK:
            _FX_CACHE[key] = (rate, now)
    return rate


def fetch_amazon_alt_price(alt_url):
    """Pobiera cenę z innego rynku Amazona (bez renderu). Zwraca dict
    {price, currency, unavailable} albo None, gdy strony/produktu brak."""
    try:
        html_text = fetch_html(alt_url, timeout=15)
    except Exception:
        return None
    if looks_blocked(html_text):
        return None
    _, price, currency, unavailable = extract_product(html_text, alt_url)
    return {"price": price, "currency": currency, "unavailable": unavailable}


def compare_amazon_marketplaces(url, base_currency, anchor_price=None):
    """Dla produktu z Amazona UE pobiera ceny tego samego ASIN-u z pozostałych
    rynków. Zwraca listę dictów: tld, label, flag, currency, price, converted,
    unavailable, url. `converted` to cena przeliczona na base_currency.
    `anchor_price` (cena bieżąca produktu w base_currency) służy do odrzucania
    skrajnie odbiegających cen — błędów ekstrakcji (np. cena promocji „kup 4”)."""
    asin = amazon_asin(url)
    own = amazon_tld(url)
    if not asin:
        return []
    # Kursy pobieramy RAZ na walutę (a nie per rynek) — jedna nieudana próba
    # nie wyłączy wtedy pojedynczego rynku (np. SEK pobierany dotąd raz).
    rates = {}
    if base_currency:
        for c in {cur for tld, cur, *_ in AMAZON_EU if tld != own}:
            rates[c] = fx_rate(c, base_currency)
    raw = []
    for tld, cur, label, flag in AMAZON_EU:
        if tld == own:
            continue
        alt_url = f"https://www.amazon.{tld}/dp/{asin}"
        info = fetch_amazon_alt_price(alt_url)
        if info is None:
            continue
        price = info["price"]
        unavailable = info["unavailable"]
        if price is None and not unavailable:
            continue                      # produktu nie ma na tym rynku
        # Waluta rynku jest stała i znana z tabeli (np. se→SEK, com.be→EUR).
        # Wykrywanie ze strony bywa błędne (amazon.se mylony z USD), więc ufamy
        # tabeli, nie HTML-owi.
        cur2 = cur
        converted = None
        if price is not None and base_currency:
            rate = rates.get(cur2)
            if rate is None:                  # awaryjnie spróbuj jeszcze raz
                rate = fx_rate(cur2, base_currency)
                rates[cur2] = rate
            if rate is not None:
                converted = price * rate
        raw.append({
            "tld": tld, "label": label, "flag": flag, "currency": cur2,
            "price": price, "converted": converted,
            "unavailable": unavailable, "url": alt_url,
        })
        time.sleep(random.uniform(0.1, 0.35))   # delikatny rozrzut

    # Filtr absurdów: ten sam ASIN ma na rynkach UE zbliżoną cenę. Punkt
    # odniesienia: mediana kohorty (≥3 cen) albo cena bieżąca. Ceny ponad 3×
    # wyższe/niższe to niemal na pewno błąd ekstrakcji (wariant/„kup 4”) — pomijamy.
    convs = sorted(a["converted"] for a in raw if a["converted"] is not None)
    anchor = None
    if len(convs) >= 3:
        anchor = convs[len(convs) // 2]            # mediana (odporna na 1 odstający)
    elif anchor_price:
        anchor = anchor_price
    if not anchor:
        return raw
    out = []
    for a in raw:
        cv = a["converted"]
        if cv is not None and (cv > anchor * 3 or cv < anchor / 3):
            continue                               # odrzuć skrajnie odstającą cenę
        out.append(a)
    return out


def extract_image(html_text, url):
    """Znajduje URL zdjęcia produktu (og:image / twitter:image / Amazon)."""
    try:
        soup = BeautifulSoup(html_text, "html.parser")
    except Exception:
        return None
    for attrs in ({"property": "og:image"}, {"property": "og:image:url"},
                  {"property": "og:image:secure_url"},
                  {"name": "twitter:image"}, {"name": "twitter:image:src"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return urljoin(url, tag["content"].strip())
    img = soup.select_one("#landingImage, #imgBlkFront, #main-image, #ebooksImgBlkFront")
    if img:
        src = img.get("data-old-hires") or img.get("src")
        if src and not src.startswith("data:"):
            return urljoin(url, src.strip())
    return None


def fetch_bytes(url, timeout=15):
    """Pobiera surowe bajty (np. obrazek). Zwraca bytes albo None."""
    if not url:
        return None
    if _CURL_OK:
        try:
            r = _curl_requests.get(url, impersonate="chrome", timeout=timeout,
                                   allow_redirects=True)
            if getattr(r, "status_code", 0) < 400 and r.content:
                return r.content
        except Exception:
            pass
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def extract_product(html_text, url):
    """Zwraca (nazwa, cena_float|None, kod_waluty, niedostępny_bool)."""
    soup = BeautifulSoup(html_text, "html.parser")
    amazon = is_amazon(url)
    strict = is_strict_host(url)

    # Niedostępność liczymy NA POCZĄTKU (przed _strip_noise, które usuwa <script>
    # potrzebne do odczytu JSON-LD). Jest też bramką dla luźnego fallbacku ceny.
    unavailable = is_unavailable(soup, url)

    name = extract_name(soup)
    price = None
    currency = None

    # 1) Amazon: ścisłe ID buyboxa; AliExpress: osadzony JSON (runParams)
    if amazon:
        ap, ac = amazon_price(soup)
        if ap is not None:
            price, currency = ap, ac
    elif is_aliexpress(url):
        ap, ac = aliexpress_price(html_text, soup)
        if ap is not None:
            price, currency = ap, ac

    # 2) meta-tagi (product:price / og:price / itemprop)
    if price is None:
        for attrs in META_PRICE_KEYS:
            v = _meta(soup, attrs)
            if v is not None:
                p = parse_price_string(v)
                if p is not None:
                    price = p
                    break
    for attrs in META_CURRENCY_KEYS:
        v = _meta(soup, attrs)
        if v:
            currency = currency or v.strip().upper()[:3]
            break

    # 3) JSON-LD (schema.org Product/Offer)
    ld_name, ld_price, ld_cur = jsonld_product(soup)
    if not name and ld_name:
        name = ld_name
    if price is None and ld_price is not None:
        price = ld_price
        currency = currency or (str(ld_cur).upper()[:3] if ld_cur else None)

    # 4) cena z opisu meta ("...za 95 zł...") — pewne źródło głównej oferty,
    #    kluczowe dla Allegro (brak JSON-LD i zahaszowane klasy CSS).
    if price is None:
        dp, dc = price_from_description(soup)
        if dp is not None:
            price = dp
            currency = currency or dc

    # 5) Amazon: luźny fallback (cena w obrębie kolumny produktu) WYŁĄCZNIE gdy
    #    produkt jest dostępny — inaczej złapalibyśmy cenę z karuzeli rekomendacji.
    if amazon and price is None and not unavailable:
        ap, ac = amazon_price_loose(soup)
        if ap is not None:
            price, currency = ap, ac

    # 6) heurystyka po wyczyszczeniu szumu — tylko poza serwisami "strict".
    #    Na Amazonie/Allegro globalny skan łapie ceny z karuzel polecanych
    #    ofert, więc tam wolimy brak ceny niż losową.
    _strip_noise(soup)
    body_text = soup.get_text(" ", strip=True)
    if price is None and not strict:
        price = heuristic_price(soup, body_text)

    if not currency:
        currency = infer_currency(body_text[:6000])

    if name:
        name = html.unescape(re.sub(r"\s+", " ", name)).strip()
    return name, price, currency or "", (price is None and unavailable)


# Frazy „Omnibus” (najniższa cena z 30 dni) — PL + rynki UE, które porównujemy.
# Każda kończy się słowem oznaczającym dni, więc liczba PO frazie to szukana cena.
_OMNIBUS_PHRASES = (
    "najniższa cena z 30 dni",
    "najniższa cena z ostatnich 30 dni",
    "niedrigster preis der letzten 30 tage",
    "prix le plus bas des 30 derniers jours",
    "prezzo più basso degli ultimi 30 giorni",
    "precio más bajo de los últimos 30 días",
    "laagste prijs van de afgelopen 30 dagen",
    "lowest price in the last 30 days",
    "lowest price (last 30 days)",
)


def extract_omnibus(html_text):
    """Najniższa cena z 30 dni (dyrektywa Omnibus) ze strony oferty — best effort.
    Zwraca float albo None, gdy strona jej nie podaje / nie da się odczytać."""
    if not html_text:
        return None
    try:
        text = BeautifulSoup(html_text, "html.parser").get_text(" ", strip=True)
    except Exception:
        text = html_text
    low = text.lower()
    for phrase in _OMNIBUS_PHRASES:
        i = low.find(phrase)
        if i == -1:
            continue
        end = i + len(phrase)
        val = parse_price_string(text[end:end + 120])   # liczba zaraz po frazie
        if val is not None:
            return val
        val = parse_price_string(text[max(0, i - 60):i])  # czasem cena przed
        if val is not None:
            return val
    return None


def _is_ceneo(url):
    return "ceneo." in (urlparse(url).netloc or "").lower()


def _ceneo_price_from_format(node):
    """Cena z <span class="price-format"><span class="value">365</span>
    <span class="penny">,40</span>zł</span>."""
    if node is None:
        return None
    val = node.select_one(".value")
    if not val:
        return None
    pen = node.select_one(".penny")
    s = val.get_text(strip=True) + (pen.get_text(strip=True) if pen else "")
    return parse_price_string(s + " zł")


def extract_ceneo_offers(html_text, url):
    """Lista ofert sklepów ze strony produktu Ceneo: [{shop, price, currency}].
    Czyta widoczne kontenery ofert (div.product-offer__container) — nazwa sklepu
    z alt logo, cena z głównego wiersza oferty (pomija warianty i reklamy)."""
    if not html_text or not _is_ceneo(url):
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    offers, seen = [], set()
    for cont in soup.select("div.product-offer__container"):
        logo = cont.select_one(".product-offer__logo img, .store-logo img")
        shop = (logo.get("alt") or "").strip() if logo else ""
        if not shop:
            link = cont.select_one("a[href*='sklepy/']")
            if link and link.get("href"):
                # .../sklepy/Woliniusz-s3200#... -> "Woliniusz"
                seg = link["href"].split("sklepy/", 1)[1].split("#")[0]
                shop = re.sub(r"-s\d+$", "", seg).replace("-", " ").strip()
        price_block = cont.select_one(".product-offer__product__price")
        price = _ceneo_price_from_format(
            price_block.select_one(".price-format") if price_block else None)
        if not shop or price is None:
            continue
        # link do oferty: atrybut data-click-url (np. „151471588;3200-0v.htm”
        # albo „Click/Offer/?e=…”) — dosklejamy do bazy Ceneo
        dcu = (cont.get("data-click-url") or "").strip()
        if dcu.startswith("http"):
            offer_url = dcu
        elif dcu.startswith("/"):
            offer_url = "https://www.ceneo.pl" + dcu
        elif dcu:
            offer_url = "https://www.ceneo.pl/" + dcu
        else:
            offer_url = url
        key = (shop.lower(), round(price, 2))
        if key in seen:
            continue
        seen.add(key)
        offers.append({"shop": shop, "price": price, "currency": "PLN",
                       "url": offer_url})
    offers.sort(key=lambda x: x["price"])
    return offers[:6]


_CENEO_PID_RE = re.compile(r"^/(\d{5,})(?:[;#?].*)?$")


def _qty_from_name(name):
    """Liczba sztuk z nazwy: „4 szt", „4szt", „czteropak", „4-pack", „x4" …
    Zwraca int albo None, gdy nie da się jednoznacznie ustalić."""
    s = (name or "").lower().replace("\u00a0", " ")
    m = re.search(r"(\d+)\s*(?:szt|sztuk|pak|pack)\b", s)
    if m:
        return int(m.group(1))
    m = re.search(r"\bx\s*(\d+)\b", s)
    if m:
        return int(m.group(1))
    words = {"jedno": 1, "dwu": 2, "dwó": 2, "trój": 3, "troj": 3, "cztero": 4,
             "pięcio": 5, "piecio": 5, "sześcio": 6, "szescio": 6,
             "ośmio": 8, "osmio": 8}
    for w, n in words.items():
        if w + "pak" in s:
            return n
    return None


def _ceneo_search_price(cont):
    """Cena „od …" z wiersza wyniku — najpierw struktura .price-format,
    potem awaryjnie pierwsza kwota „… zł" w tekście wiersza."""
    if cont is None:
        return None
    pf = cont.select_one(".price-format")
    p = _ceneo_price_from_format(pf) if pf else None
    if p is not None:
        return p
    txt = cont.get_text(" ", strip=True).replace("\u00a0", " ")
    m = re.search(r"(\d[\d ]{0,9},\d{2})\s*zł", txt)
    return parse_price_string(m.group(1) + " zł") if m else None


def _is_result_row(tag):
    """Czy znacznik to wiersz wyniku (np. cat-prod-row), a NIE jego pod-element
    (cat-prod-row__name) — inaczej cena z wiersza byłaby poza zasięgiem."""
    for c in (tag.get("class") or []):
        if "__" in c:                       # pod-element BEM
            continue
        cl = c.lower()
        if ("prod" in cl and "row" in cl) or cl in ("grid-cell", "offer-box"):
            return True
    return False


def parse_ceneo_search(html_text):
    """Lista produktów ze strony wyników Ceneo: [{url, name, price, qty}].
    Ogranicza się do głównej listy wyników (pomija box „Polecane" i sidebar),
    a w razie wątpliwości i tak chroni nas późniejsze dopasowanie po cenie."""
    soup = BeautifulSoup(html_text, "html.parser")
    scope = (soup.select_one(".category-list-body")
             or soup.select_one(".js_category-list-body")
             or soup.select_one(".products-list") or soup)
    cands, seen = [], set()
    for a in scope.select("a[href]"):
        m = _CENEO_PID_RE.match((a.get("href") or "").strip())
        if not m:
            continue
        pid = m.group(1)
        if pid in seen:
            continue
        nm = a.get_text(" ", strip=True)
        if not nm or len(nm) < 3:           # pomijamy linki-obrazki bez nazwy
            continue
        seen.add(pid)
        cont = (a.find_parent(_is_result_row)
                or a.find_parent(["article", "li"]) or a.parent)
        cands.append({"url": "https://www.ceneo.pl/" + pid, "name": nm,
                      "price": _ceneo_search_price(cont),
                      "qty": _qty_from_name(nm)})
        if len(cands) >= 30:
            break
    return cands


def _pick_best_ceneo(cands, ref_name, ref_price):
    """Wybór najlepszego dopasowania: najpierw zgodna LICZBA SZTUK (czteropak
    itp.), potem najbliższa CENA odniesienia (cena monitorowanego produktu).
    Cena jest najlepszym dyskryminatorem — Ceneo pokazuje „od <najniższa>"."""
    if not cands:
        return None
    pool = cands
    ref_qty = _qty_from_name(ref_name)
    if ref_qty is not None:
        same = [c for c in cands if c.get("qty") == ref_qty]
        if same:
            pool = same
        else:                                # odrzuć jawnie inną liczbę sztuk
            pool = [c for c in cands if c.get("qty") in (None, ref_qty)] or cands
    if ref_price:
        priced = [c for c in pool if c.get("price")]
        if priced:
            # w sensownym przedziale cenowym, a z nich najbliższa cena
            band = [c for c in priced
                    if 0.5 * ref_price <= c["price"] <= 2.0 * ref_price]
            chosen = band or priced
            chosen.sort(key=lambda c: abs(c["price"] - ref_price))
            return chosen[0]
    return pool[0]


def find_ceneo_product_url(name, ref_price=None):
    """Pojedyncze zapytanie do wyszukiwarki Ceneo i wybór NAJLEPIEJ pasującego
    produktu (po liczbie sztuk + cenie odniesienia), nie „pierwszego z brzegu".

    Zwraca (url, reason):
        (url, "ok")        – znaleziono stronę produktu Ceneo,
        (None, "blocked")  – ochrona antybotowa / błąd sieci,
        (None, "empty")    – brak wyników albo nie rozpoznano linku,
        (None, "no-query") – pusta fraza.

    UWAGA: to endpoint WYSZUKIWARKI (ta sama kategoria co listing Allegro).
    Używamy go wyłącznie raz na kliknięcie użytkownika (bez tła, bez pętli),
    z fingerprintem TLS Chrome'a, by ograniczyć ryzyko blokady DataDome."""
    q = clean_query(name)
    if not q:
        return None, "no-query"
    search_url = "https://www.ceneo.pl/;szukaj-" + quote_plus(q)
    try:
        html_text = fetch_html(search_url)
    except requests.RequestException:
        return None, "blocked"
    if not html_text or looks_blocked(html_text):
        return None, "blocked"
    cands = parse_ceneo_search(html_text)
    if not cands:
        return None, "empty"
    best = _pick_best_ceneo(cands, name, ref_price)
    return (best["url"], "ok") if best else (None, "empty")


def heuristic_price(soup, body_text=None):
    """Awaryjne szukanie ceny po klasach/id/itemprop oraz w pobliżu waluty.
    Działa na drzewie po usunięciu rekomendacji (patrz _strip_noise)."""
    pat = re.compile(r"price|cena|kwota|amount", re.I)
    for el in soup.find_all(attrs={"itemprop": "price"}):
        p = parse_price_string(el.get("content") or el.get_text(" ", strip=True))
        if p is not None:
            return p
    for finder in (soup.find_all(class_=pat), soup.find_all(id=pat)):
        for el in finder:
            p = parse_price_string(el.get_text(" ", strip=True))
            if p is not None:
                return p
    text = body_text if body_text is not None else soup.get_text(" ", strip=True)
    m = re.search(
        r"(\d[\d\s\u00a0.,]{0,12}\d|\d)\s*(zł|PLN|€|EUR|\$|USD|£|GBP|CHF|Kč)",
        text,
    )
    if m:
        return parse_price_string(m.group(1))
    m = re.search(r"(€|\$|£)\s*(\d[\d\s\u00a0.,]{0,12}\d|\d)", text)
    if m:
        return parse_price_string(m.group(2))
    return None


def _raise_for_status(code):
    if code and code >= 400:
        resp = type("Resp", (), {"status_code": code})()
        raise requests.HTTPError(f"HTTP {code}", response=resp)


def fetch_html(url, timeout=20):
    """Pobiera HTML. Najpierw curl_cffi z fingerprintem TLS/JA3 prawdziwego
    Chrome (omija filtr sieciowy DataDome, którego nie przejdzie zwykły
    requests), z fallbackiem na requests. Rzuca HTTPError przy 4xx/5xx."""
    if _CURL_OK:
        try:
            # impersonate sam ustawia spójne nagłówki + TLS Chrome'a;
            # nie nadpisujemy UA, by nie tworzyć niespójności fingerprintu
            r = _curl_requests.get(
                url, impersonate="chrome",
                headers={"Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7"},
                timeout=timeout, allow_redirects=True,
            )
        except Exception:
            r = None
        if r is not None:
            _raise_for_status(getattr(r, "status_code", 0))
            text = getattr(r, "text", "") or ""
            if text:
                return text

    r = requests.get(url, headers=HTTP_HEADERS, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text



# ============================================================================
#  Renderowanie JS (Playwright) — best-effort, w osobnym procesie.
#  Na darmowym hostingu zwykle niedostępne (brak chromium / blokady IP);
#  aplikacja działa wtedy bez renderu (curl_cffi). Lokalnie / na VPS z
#  zainstalowanym `playwright install chromium` render włącza się automatycznie.
# ============================================================================

_render_lock = threading.Lock()


def playwright_available():
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _pw_profile_dir():
    base = os.environ.get("PRICEMON_DATA_DIR") or str(Path.home() / ".pricemon")
    d = Path(base) / "pw-profile"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return ""
    return str(d)


def _render_worker_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_worker.py")


def fetch_html_rendered(url, headless=True):
    """Render JS w podprocesie (render_worker.py + Playwright). Zwraca
    (html|None, błąd|None). Headed nie ma sensu na serwerze — zawsze headless."""
    if not playwright_available():
        return None, "playwright-missing"
    worker = _render_worker_path()
    if not os.path.exists(worker):
        return None, "playwright-missing"
    timeout = 90
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    extra = {}
    if os.name == "nt":
        extra["creationflags"] = 0x08000000     # CREATE_NO_WINDOW
    cmd = [sys.executable, worker, url, USER_AGENT, "1", _pw_profile_dir()]
    with _render_lock:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                  env=env, **extra)
        except subprocess.TimeoutExpired:
            return None, "timeout"
        except Exception as e:
            return None, str(e)[:200]
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    if proc.returncode == 0 and out and len(out) > 500:
        return out, None
    errl = err.strip().lower()
    if proc.returncode == 3:
        return None, "playwright-missing"
    if "playwright install" in errl or "executable doesn't exist" in errl:
        return None, "browser-missing"
    return None, err.strip()[:200] or "render-failed"

_BLOCK_MARKERS = (
    "zostałeś zablokowany", "you have been blocked", "access denied",
    "access to this page has been denied", "captcha-delivery",
    "datadome", "px-captcha", "enable javascript and cookies to continue",
    "are you a robot", "unusual traffic", "verify you are a human",
)


def looks_blocked(html_text):
    """Czy zwrócona strona to strona-blokada ochrony antybotowej (DataDome itp.)?"""
    if not html_text:
        return False
    low = html_text[:8000].lower()
    return any(m in low for m in _BLOCK_MARKERS)


# Oferta zakończona/wycofana (NIE blokada) — głównie Allegro, plus warianty.
_OFFER_ENDED_MARKERS = (
    "sprzedaż zakończona",
    "skończyła się. sprawdź inne oferty",
    "ta oferta została zakończona",
    "to ogłoszenie się zakończyło",
    "oferta archiwalna",
    "oferta wygasła",
)


def is_offer_ended(html_text):
    """True, gdy strona to zakończona/wygasła oferta (np. Allegro „Sprzedaż
    zakończona”). Szukamy w tekście BEZ tagów, bo fraza bywa rozbita na <span>-y.
    Taki przypadek to niedostępność, a nie blokada antybotowa."""
    if not html_text:
        return False
    try:
        text = BeautifulSoup(html_text, "html.parser").get_text(" ", strip=True).lower()
    except Exception:
        text = html_text.lower()
    return any(m in text for m in _OFFER_ENDED_MARKERS)


def resolve_product(url, force_headed=False):
    """requests -> render headless. Render w widocznym oknie tylko na żądanie
    (force_headed=True) — automatyczne otwieranie okna jest wyłączone.

    Zwraca dict: name, price, currency, rendered, unavailable, js_available,
    needs_js, render_error, http_blocked.
    """
    name = price = currency = None
    unavailable = False
    ended = False
    http_error = None
    blocked = False
    image = None
    omnibus = None
    offers = []

    if not force_headed:
        try:
            html_text = fetch_html(url)
            if is_offer_ended(html_text):
                unavailable = True           # zakończona oferta = niedostępne
                ended = True
            elif looks_blocked(html_text):
                blocked = True
            else:
                name, price, currency, unavailable = extract_product(html_text, url)
                image = extract_image(html_text, url)
                omnibus = extract_omnibus(html_text)
                offers = extract_ceneo_offers(html_text, url)
        except requests.RequestException as e:
            http_error = e

    rendered = False
    render_error = None

    def _try_render(headless):
        nonlocal rendered, render_error, name, price, currency, unavailable, blocked, image, omnibus, ended, offers
        rhtml, render_error = fetch_html_rendered(url, headless=headless)
        if rhtml:
            rendered = True
            if is_offer_ended(rhtml):
                unavailable = True
                ended = True
                blocked = False              # to nie blokada, lecz koniec oferty
                render_error = None
                return
            if looks_blocked(rhtml):
                blocked = True
                render_error = "blocked"
                return
            r_name, r_price, r_cur, r_unavail = extract_product(rhtml, url)
            name = name or r_name
            if r_price is not None:
                price, currency = r_price, r_cur or currency
            unavailable = unavailable or r_unavail
            image = image or extract_image(rhtml, url)
            omnibus = omnibus or extract_omnibus(rhtml)
            if not offers:
                offers = extract_ceneo_offers(rhtml, url)

    if price is None and not unavailable:
        _try_render(headless=not force_headed)

    if blocked and render_error not in ("blocked",):
        render_error = "blocked"

    # requests padł, render niemożliwy/odpadł -> przekaż błąd HTTP
    if price is None and not unavailable and not blocked \
            and http_error is not None and not rendered:
        if render_error in (None, "playwright-missing"):
            raise http_error

    return {
        "name": name, "price": price, "currency": currency,
        "rendered": rendered, "unavailable": unavailable,
        "js_available": playwright_available(),
        "needs_js": (price is None and not unavailable and is_js_required_host(url)),
        "render_error": render_error,
        "http_blocked": bool(http_error),
        "blocked": blocked,
        "ended": ended,
        "image": image,
        "omnibus": omnibus,
        "offers": offers,
    }


# ============================================================================
#  CZĘŚĆ 2.  Model danych + trwałość
# ============================================================================

@dataclass
class Product:
    url: str
    name: str = ""
    currency: str = ""
    initial_price: float = None
    current_price: float = None
    date_added: str = ""
    last_checked: str = ""
    history: list = field(default_factory=list)   # [{"t": iso, "p": float}]
    alts: list = field(default_factory=list)       # ceny z innych Amazonów UE
    alts_checked: str = ""
    favorite: bool = False
    image_url: str = ""
    omnibus_price: float = None      # najniższa cena z 30 dni (Omnibus), jeśli jest
    shop_offers: list = field(default_factory=list)   # oferty sklepów (Ceneo)
    # pola ulotne (nie zapisywane)
    status: str = "new"          # new | fetching | ok | error | unavailable
    error: str = ""

    def to_dict(self):
        return {
            "url": self.url, "name": self.name, "currency": self.currency,
            "initial_price": self.initial_price, "current_price": self.current_price,
            "date_added": self.date_added, "last_checked": self.last_checked,
            "history": self.history[-1000:],
            "alts": self.alts, "alts_checked": self.alts_checked,
            "favorite": self.favorite, "image_url": self.image_url,
            "omnibus_price": self.omnibus_price,
            "shop_offers": self.shop_offers[:8],
            "ended": self.status == "ended",
        }

    @staticmethod
    def from_dict(d):
        p = Product(url=d.get("url", ""))
        p.name = d.get("name", "")
        p.currency = d.get("currency", "")
        p.initial_price = d.get("initial_price")
        p.current_price = d.get("current_price")
        p.date_added = d.get("date_added", "")
        p.last_checked = d.get("last_checked", "")
        p.history = dedupe_history(d.get("history", []))
        alts = d.get("alts", []) or []
        mismatch = False
        for a in alts:
            tcur = _EU_CUR.get(a.get("tld", ""))
            if tcur and a.get("currency") != tcur:
                a["currency"] = tcur          # popraw błędną walutę (np. „USD"→SEK)
                a["converted"] = None         # policzono złym kursem — unieważnij
                mismatch = True
        p.alts = alts
        # wymuś świeże przeliczenie przy najbliższym sprawdzeniu, jeśli coś poprawiono
        p.alts_checked = "" if mismatch else d.get("alts_checked", "")
        p.favorite = bool(d.get("favorite", False))
        p.image_url = d.get("image_url", "")
        p.omnibus_price = d.get("omnibus_price")
        p.shop_offers = d.get("shop_offers", []) or []
        p.status = "ok" if p.current_price is not None else "new"
        if d.get("ended"):
            p.status = "ended"
        return p


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def fmt_dt(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m %H:%M")
    except ValueError:
        return iso


def fmt_money(value, currency=""):
    if value is None:
        return "—"
    s = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    sym = CURRENCY_SYMBOL.get(currency, currency or "")
    if currency in ("USD", "GBP") or sym in ("$", "£"):
        return f"{sym}{s}"
    return f"{s} {sym}".strip()


def change_text(p):
    if p.initial_price and p.current_price is not None and p.initial_price > 0:
        diff = p.current_price - p.initial_price
        if abs(diff) < 0.005:
            return "0%"
        sign = "+" if diff > 0 else "−"
        pct = abs(diff) / p.initial_price * 100
        pct_s = f"{pct:.1f}".replace(".", ",")
        return f"{sign}{fmt_money(abs(diff), p.currency)}  ({sign}{pct_s}%)"
    return "—"


def dedupe_history(hist):
    """Zostawia tylko punkty ZMIANY ceny (pomija powtórzenia tej samej ceny).
    Normalizuje też starsze dane, gdzie zapisywano każdy odczyt."""
    out = []
    last = None
    for e in hist or []:
        p = e.get("p")
        if p is None:
            continue
        if last is None or abs(last - p) >= 0.005:
            out.append({"t": e.get("t", ""), "p": p})
            last = p
    return out


def plural_zmiana(n):
    """Polska odmiana: 1 zmiana, 2-4 zmiany, 0/5+ zmian (z wyjątkami nastek)."""
    if n == 1:
        return "zmiana"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return "zmiany"
    return "zmian"


def ceneo_offers_html(p):
    """Dymek: oferty sklepów ze strony produktu Ceneo (najtańsze u góry)."""
    valid = [o for o in (p.shop_offers or []) if o.get("price") is not None]
    if not valid:
        return None
    rows = []
    for o in sorted(valid, key=lambda x: x["price"])[:6]:
        price = fmt_money(o["price"], o.get("currency") or "PLN")
        shop = html.escape(o.get("shop", "") or "")
        url = o.get("url", "")
        shop_html = (f"<a href='{url}' style='color:#7db0d0;"
                     f"text-decoration:none;'>{shop}</a>" if url else shop)
        rows.append(f"<span style='color:#5fb56f'>{price}</span> &nbsp; "
                    f"{shop_html}")
    head = "<b>Oferty sklepów (Ceneo)</b>"
    return head + "<br>" + "<br>".join(rows)


def history_html(p):
    """Rich text z historią zmian ceny (czerwony = drożej, zielony = taniej).
    Pokazuje ostatnie ~15 punktów; starsze sygnalizuje skrótem."""
    rows_all = []
    prev = None
    for e in p.history or []:
        price = e.get("p")
        if price is None:
            continue
        delta = None if prev is None else price - prev
        rows_all.append((e.get("t", ""), price, delta))
        prev = price
    if not rows_all:
        return None
    shown = rows_all[-15:]
    truncated = len(rows_all) - len(shown)
    # liczba ZMIAN = liczba punktów − 1 (pierwszy punkt to cena wyjściowa)
    changes = len(rows_all) - 1
    lines = [f"<b>Historia ceny</b>  ·  {changes} {plural_zmiana(changes)}"]
    if truncated > 0:
        lines.append(f"<span style='color:#8a8a8a'>… starsze pominięto "
                     f"({truncated})</span>")
    for t, price, delta in shown:
        when = fmt_dt(t) or "—"
        s = f"{when}&nbsp;&nbsp; {fmt_money(price, p.currency)}"
        if delta is not None and abs(delta) >= 0.005:
            color = "#e0705f" if delta > 0 else "#5fb56f"
            sign = "+" if delta > 0 else "−"
            s += (f"&nbsp;&nbsp; <span style='color:{color}'>{sign}"
                  f"{fmt_money(abs(delta), p.currency)}</span>")
        lines.append(s)
    return "<br>".join(lines)


def domain(url):
    try:
        host = urlparse(url).netloc
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return url


def has_cheaper_alt(p):
    """True, jeśli na którymś innym Amazonie produkt jest tańszy (po przeliczeniu)."""
    base = p.current_price
    if not base or not p.alts:
        return False
    for a in p.alts:
        conv = a.get("converted")
        if conv is not None and conv < base * 0.998:
            return True
    return False


def best_cheaper_alt(p):
    """Najniższa znaleziona cena TAŃSZA niż bieżąca (po przeliczeniu na walutę
    bazową). Zwraca dict {converted, tld, currency, price, flag, url} albo None.
    Na razie źródłem są inne Amazony UE; docelowo trafią tu też znalezione oferty."""
    base = p.current_price
    if not base or not p.alts:
        return None
    best = None
    for a in p.alts:
        conv = a.get("converted")
        if conv is None or conv >= base - 0.005:
            continue
        if best is None or conv < best["converted"]:
            tld = a.get("tld", "")
            best = {
                "converted": conv, "tld": tld,
                "currency": _EU_CUR.get(tld) or a.get("currency") or "",
                "price": a.get("price"), "flag": a.get("flag", ""),
                "url": a.get("url", ""),
            }
    return best


def alts_html(p, links=False):
    """Buduje rich text z cenami tego samego produktu na innych Amazonach UE;
    kolory: czerwony = drożej, zielony = taniej. links=True → nazwy rynków jako
    klikalne odnośniki (dla panelu po kliknięciu)."""
    if not p.alts:
        return None
    base = p.current_price
    base_cur = p.currency
    rows = []
    for a in p.alts:
        tld = a.get("tld", "")
        # waluta rynku jest stała (po tld) — odporne na błędnie zapisane dane
        cur = _EU_CUR.get(tld) or a.get("currency") or ""
        dom = f"amazon.{tld}"
        flag = a.get("flag", "")
        url = a.get("url", "")
        dom_html = (f"<a href='{url}' style='color:#7db0d0;text-decoration:none;'>{dom}</a>"
                    if links and url else dom)
        if a.get("unavailable") and a.get("price") is None:
            rows.append(f"{flag} {dom_html}: "
                        f"<span style='color:#c2a05a'>niedostępny</span>")
            continue
        price = a.get("price")
        if price is None:
            continue
        native = fmt_money(price, cur)
        conv = a.get("converted")
        color = "#cfcfcf"
        note = ""
        if conv is not None and base:
            if conv > base * 1.002:
                color, note = "#e0705f", "  ▲ drożej"
            elif conv < base * 0.998:
                color, note = "#5fb56f", "  ▼ taniej"
            else:
                note = "  ≈ tyle samo"
        conv_s = ""
        if conv is not None and cur != (base_cur or ""):
            conv_s = f" (≈{fmt_money(conv, base_cur)})"
        rows.append(f"{flag} {dom_html}: "
                    f"<span style='color:{color}'>{native}{conv_s}{note}</span>")
    if not rows:
        return None
    # wiersz odniesienia: cena bazowa i zmiana (kolumny „Baza”/„Zmiana”,
    # które kafelek zasłania)
    bp = p.initial_price
    base_price_s = fmt_money(bp, base_cur) if bp is not None else "—"
    base_line = (f"<span style='color:#8a8a8a'>Cena bazowa:</span> "
                 f"<span style='color:#e6e6e6'>{base_price_s}</span>"
                 f"  <span style='color:#8a8a8a'>·  zmiana:</span> "
                 f"<span style='color:#cfcfcf'>{change_text(p)}</span>")
    when = f"  ·  {fmt_dt(p.alts_checked)}" if p.alts_checked else ""
    head = f"<b>Ceny na innych Amazonach (UE)</b>{when}"
    hint = ("<br><span style='color:#8a8a8a'>kliknij nazwę rynku, aby otworzyć</span>"
            if links else "")
    return head + "<br>" + base_line + "<br>" + "<br>".join(rows) + hint

def data_path():
    """Ścieżka pliku z danymi. Na hostingu efemerycznym (Streamlit Cloud) plik
    znika po restarcie — trwałość zapewnia eksport/import JSON. Katalog można
    nadpisać zmienną PRICEMON_DATA_DIR (np. wolumen na VPS)."""
    base = os.environ.get("PRICEMON_DATA_DIR") or str(Path.home() / ".pricemon")
    Path(base).mkdir(parents=True, exist_ok=True)
    return Path(base) / "pricemon.json"
