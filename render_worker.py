#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_worker.py — renderuje stronę Playwrightem i wypisuje HTML na stdout.

Wywoływane przez pricemon_core.fetch_html_rendered() jako osobny proces:
    python render_worker.py <url> <user_agent> <headless 1/0> <profile_dir>

Osobny proces jest celowy: sync API Playwrighta nie działa w wątku roboczym
(np. ScriptRunner Streamlita) — „signal only works in main thread”.
Kod renderowania 1:1 z desktopowego PriScannera.
"""
import os as _os
import sys as _sys


def main(args):
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
        a = ["--disable-blink-features=AutomationControlled",
             "--disable-dev-shm-usage"]
        if _sys.platform.startswith("linux") and getattr(_os, "geteuid", None) \
                and _os.geteuid() == 0:
            a.append("--no-sandbox")
        kw = dict(headless=headless, args=a, locale="pl-PL",
                  timezone_id="Europe/Warsaw", viewport={"width": 1366, "height": 900},
                  ignore_default_args=["--enable-automation"])
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
    for channel in ("chromium", "chrome", "msedge", None):
        try:
            html_out = run(channel)
            break
        except Exception as e:
            last_err = str(e)
            continue
    if html_out:
        try:
            _os.write(1, html_out.encode("utf-8", "replace"))
        except Exception:
            pass
        _sys.exit(0)
    try:
        _os.write(2, last_err.encode("utf-8", "replace")[:500])
    except Exception:
        pass
    _sys.exit(2)


if __name__ == "__main__":
    main(_sys.argv[1:])
