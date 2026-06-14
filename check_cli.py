#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_cli.py — bezgłowe sprawdzenie wszystkich produktów z pliku danych.
Do użycia w cronie na VPS-ie (prawdziwy monitoring 24/7), np.:

    0 */4 * * *  cd /opt/priscanner && PRICEMON_DATA_DIR=/opt/priscanner/data \
                 /opt/priscanner/.venv/bin/python check_cli.py >> check.log 2>&1

Aktualizuje ten sam pricemon.json, który czyta aplikacja Streamlit.
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pricemon_core as core
from pricemon_core import (Product, resolve_product, compare_amazon_marketplaces,
                           is_eu_amazon, now_iso, data_path)


def fetch(url):
    try:
        return resolve_product(url)
    except Exception as e:
        return {"price": None, "_error": str(e)}


def apply(p, r):
    if r.get("ended"):
        p.status = "ended"; p.current_price = None
    elif r.get("price") is None and r.get("unavailable"):
        p.status = "unavailable"; p.current_price = None; p.initial_price = None
    elif r.get("price") is None:
        p.status = "error"; p.error = r.get("_error", "brak ceny")
    else:
        p.status = "ok"; p.error = ""
        for k in ("name", "currency", "image", "omnibus", "offers"):
            v = r.get(k)
            if v:
                setattr(p, {"image": "image_url", "omnibus": "omnibus_price",
                            "offers": "shop_offers"}.get(k, k), v)
        price = r["price"]
        last = p.history[-1]["p"] if p.history else None
        if last is None or abs(last - price) >= 0.005:
            p.history.append({"t": now_iso(), "p": price})
        if p.initial_price is None:
            p.initial_price = price
        p.current_price = price
    p.last_checked = now_iso()


def main():
    path = data_path()
    if not path.exists():
        print("Brak pliku danych:", path); return
    data = json.loads(path.read_text(encoding="utf-8"))
    products = [Product.from_dict(d) for d in data.get("products", []) if d.get("url")]
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] sprawdzam {len(products)} produktów")
    results = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(fetch, p.url): p for p in products}
        for fut in as_completed(futs):
            apply(futs[fut], fut.result())
    for p in products:
        if is_eu_amazon(p.url) and p.current_price:
            try:
                p.alts = compare_amazon_marketplaces(p.url, p.currency, p.current_price) or []
                p.alts_checked = now_iso()
            except Exception:
                pass
    payload = {"interval_hours": int(data.get("interval_hours", 4)),
               "products": [p.to_dict() for p in products]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for p in products if p.status == "ok")
    print(f"  gotowe: {ok} z ceną, zapisano {path}")


if __name__ == "__main__":
    main()
