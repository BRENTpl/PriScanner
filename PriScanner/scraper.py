#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scraper.py — czysta logika PriceMon (port z aplikacji desktopowej PySide6).

Zawiera: rozpoznawanie ceny/nazwy/dostepnosci, obsluge Amazon/Allegro/AliExpress,
porownywarke Amazonow UE, przeliczanie walut, pobieranie HTML (curl_cffi -> requests
-> Playwright render w podprocesie). Bez zaleznosci od Qt ani od backendu.

Uruchomiony bezposrednio z flaga renderu dziala jako podproces Playwrighta:
    python scraper.py --pricemon-render <url> <ua> <headless> <profile_dir>
"""

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

# ----------------------------------------------------------------------------
# Zależności zewnętrzne — czytelny komunikat, jeśli czegoś brakuje
# ----------------------------------------------------------------------------
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "Brakuje zależności: {}\n"
        "Zainstaluj:  pip install requests beautifulsoup4\n".format(exc.name)
    )
    raise SystemExit(1)

# opcjonalnie: curl_cffi podszywa się pod fingerprint TLS/JA3 prawdziwego Chrome
try:
    from curl_cffi import requests as _curl_requests
    _CURL_OK = True
except Exception:
    _curl_requests = None
    _CURL_OK = False



# ============================================================================
#  CZĘŚĆ 1.  Logika: rozpoznawanie ceny / nazwy (czyste funkcje, bez Qt)
# ============================================================================

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


def extract_name(soup):
    for attrs in ({"property": "og:title"}, {"name": "twitter:title"}):
        v = _meta(soup, attrs)
        if v:
            return v
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(" ", strip=True)
        if t:
            return t
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


# --- kursy walut (frankfurter.app, ECB) z prostym cache ---
_FX_CACHE = {}            # (frm,to) -> (rate, timestamp)
_FX_LOCK = threading.Lock()


def fx_rate(frm, to):
    """Kurs przeliczeniowy frm->to (float) albo None. Cache 6 h."""
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
    try:
        r = requests.get(f"https://api.frankfurter.app/latest?from={frm}&to={to}",
                         timeout=10)
        r.raise_for_status()
        rate = r.json().get("rates", {}).get(to)
        rate = float(rate) if rate is not None else None
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


def compare_amazon_marketplaces(url, base_currency):
    """Dla produktu z Amazona UE pobiera ceny tego samego ASIN-u z pozostałych
    rynków. Zwraca listę dictów: tld, label, flag, currency, price, converted,
    unavailable, url. `converted` to cena przeliczona na base_currency."""
    asin = amazon_asin(url)
    own = amazon_tld(url)
    if not asin:
        return []
    out = []
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
        cur2 = info["currency"] or cur
        converted = None
        if price is not None and base_currency:
            rate = fx_rate(cur2, base_currency)
            if rate is not None:
                converted = price * rate
        out.append({
            "tld": tld, "label": label, "flag": flag, "currency": cur2,
            "price": price, "converted": converted,
            "unavailable": unavailable, "url": alt_url,
        })
        time.sleep(random.uniform(0.1, 0.35))   # delikatny rozrzut
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


def playwright_available():
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


# Playwright uruchamiamy w OSOBNYM PROCESIE. Powód: sync API odpalane w wątku
# roboczym QThreadPool wywala się na ustawianiu handlera sygnału
# ("signal only works in main thread"). W podprocesie działa na swoim głównym
# wątku i jest dodatkowo odizolowane od GUI.
_RENDER_FLAG = "--pricemon-render"


def _render_entry(args):
    """Uruchamiane w PONOWNIE odpalonym procesie (re-exec) z flagą
    --pricemon-render. Renderuje stronę Playwrightem i zapisuje HTML na stdout.
    Dzięki temu po spakowaniu PyInstallerem nie odpalamy `sys.executable -c`,
    czyli nie uruchamiamy w kółko kolejnych kopii aplikacji.
    args = [url, user_agent, headless('1'/'0'), profile_dir]."""
    import os as _os
    import sys as _sys
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        _sys.exit(3)

    url = args[0] if args else ""
    ua = args[1] if len(args) > 1 else ""
    headless = (len(args) < 3) or args[2] != "0"
    profile_dir = args[3] if len(args) > 3 and args[3] else None
    if not profile_dir:
        import tempfile
        profile_dir = tempfile.mkdtemp(prefix="pricemon-pw-")

    stealth = (
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "Object.defineProperty(navigator,'languages',{get:()=>['pl-PL','pl','en-US','en']});"
        "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
        "window.chrome=window.chrome||{runtime:{}};"
    )
    price_fn = r"() => /\d[\d \u00a0.,]*\s*(z\u0142|PLN|EUR|USD)/.test(document.body.innerText)"
    consent = ['[data-role="accept-consent"]', 'button:has-text("Zgadzam si\u0119")',
               'button:has-text("Akceptuj")', '#onetrust-accept-btn-handler']

    def run(channel):
        a = ["--no-sandbox", "--disable-blink-features=AutomationControlled",
             "--disable-dev-shm-usage"]
        kw = dict(headless=headless, args=a, locale="pl-PL",
                  timezone_id="Europe/Warsaw", viewport={"width": 1366, "height": 900})
        if channel:
            kw["channel"] = channel
        else:
            kw["user_agent"] = ua
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(profile_dir, **kw)
            try:
                ctx.add_init_script(stealth)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                for sel in consent:
                    try:
                        b = page.locator(sel).first
                        if b.is_visible(timeout=700):
                            b.click(timeout=1500)
                            break
                    except Exception:
                        pass
                try:
                    page.wait_for_load_state("networkidle", timeout=9000)
                except Exception:
                    pass
                try:
                    page.wait_for_function(price_fn,
                                           timeout=90000 if not headless else 6000)
                except Exception:
                    pass
                page.wait_for_timeout(800)
                return page.content()
            finally:
                ctx.close()

    html_out, last_err = None, ""
    for channel in ("chrome", "msedge", None):
        try:
            html_out = run(channel)
            break
        except Exception as e:
            last_err = str(e)
            continue
    if html_out:
        try:
            _os.write(1, html_out.encode("utf-8", "replace"))   # fd 1 = stdout
        except Exception:
            pass
        _sys.exit(0)
    try:
        _os.write(2, last_err.encode("utf-8", "replace")[:500])  # fd 2 = stderr
    except Exception:
        pass
    _sys.exit(2)


_render_lock = threading.Lock()


def _pw_profile_dir():
    d = Path.home() / ".pricemon" / "pw-profile"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return ""
    return str(d)


def fetch_html_rendered(url, headless=True):
    """Render JS w podprocesie (prawdziwy Chrome + trwały profil). Rendery
    serializujemy — wspólny profil nie może być otwarty dwukrotnie naraz.
    Podproces to PONOWNE uruchomienie tej aplikacji z flagą --pricemon-render
    (działa też po spakowaniu PyInstallerem). Zwraca (html|None, błąd|None)."""
    if not playwright_available():
        return None, "playwright-missing"
    timeout = 130 if not headless else 75
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    extra = {}
    if os.name == "nt":
        extra["creationflags"] = 0x08000000     # CREATE_NO_WINDOW
    base = ([sys.executable] if getattr(sys, "frozen", False)
            else [sys.executable, os.path.abspath(__file__)])
    cmd = base + [_RENDER_FLAG, url, USER_AGENT,
                  "1" if headless else "0", _pw_profile_dir()]
    with _render_lock:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                  env=env, **extra)
        except subprocess.TimeoutExpired:
            return None, "timeout"
        except Exception as e:                       # pragma: no cover
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


def resolve_product(url, force_headed=False):
    """requests -> render headless. Render w widocznym oknie tylko na żądanie
    (force_headed=True) — automatyczne otwieranie okna jest wyłączone.

    Zwraca dict: name, price, currency, rendered, unavailable, js_available,
    needs_js, render_error, http_blocked.
    """
    name = price = currency = None
    unavailable = False
    http_error = None
    blocked = False
    image = None

    if not force_headed:
        try:
            html_text = fetch_html(url)
            if looks_blocked(html_text):
                blocked = True
            else:
                name, price, currency, unavailable = extract_product(html_text, url)
                image = extract_image(html_text, url)
        except requests.RequestException as e:
            http_error = e

    rendered = False
    render_error = None

    def _try_render(headless):
        nonlocal rendered, render_error, name, price, currency, unavailable, blocked, image
        rhtml, render_error = fetch_html_rendered(url, headless=headless)
        if rhtml:
            rendered = True
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
        "image": image,
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
        p.history = d.get("history", []) or []
        p.alts = d.get("alts", []) or []
        p.alts_checked = d.get("alts_checked", "")
        p.favorite = bool(d.get("favorite", False))
        p.image_url = d.get("image_url", "")
        p.status = "ok" if p.current_price is not None else "new"
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
        dom = f"amazon.{a.get('tld','')}"
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
        native = fmt_money(price, a.get("currency"))
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
        if conv is not None and (a.get("currency") or "") != (base_cur or ""):
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


def normalize_url(raw):
    """Sprowadza wklejony adres do postaci kanonicznej (port z desktopu).
    Dokleja https:// gdy brak schematu, odrzuca śmieci bez hosta. Zwraca
    str albo None."""
    url = (raw or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return url


# ----------------------------------------------------------------------------
#  Punkt wejscia dla podprocesu renderujacego (Playwright).
#  fetch_html_rendered() odpala: python scraper.py --pricemon-render ...
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    if _RENDER_FLAG in sys.argv:
        i = sys.argv.index(_RENDER_FLAG)
        _render_entry(sys.argv[i + 1:])
