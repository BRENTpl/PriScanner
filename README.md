# PriScanner Web

Webowy port desktopowego **PriScannera** (PySide6 → Streamlit). Monitor cen:
wklejasz link do produktu, aplikacja rozpoznaje nazwę, cenę i walutę, śledzi
zmiany, porównuje ten sam ASIN między Amazonami UE, czyta najniższą cenę z 30 dni
(Omnibus) i oferty sklepów z Ceneo.

**Cały silnik** (`pricemon_core.py`) pochodzi 1:1 z wersji desktopowej — ta sama
logika rozpoznawania ceny, te same techniki anty-bot (`curl_cffi` z fingerprintem
TLS Chrome), te same parsery Amazon / Allegro / AliExpress / Ceneo. Format pliku
danych i eksportu JSON jest w pełni zgodny z aplikacją desktopową.

## Pliki

| plik | rola |
|---|---|
| `app.py` | UI Streamlit (motyw „Nuke”, tabela, panel akcji, import/eksport) |
| `pricemon_core.py` | silnik: scraping, model danych, formatowanie (bez Qt) |
| `render_worker.py` | render stron JS Playwrightem w osobnym procesie (opcjonalny) |
| `check_cli.py` | bezgłowe sprawdzanie do `cron` na VPS (monitoring 24/7) |
| `requirements.txt`, `packages.txt`, `.streamlit/config.toml` | konfiguracja |

## Uruchomienie lokalne

```bash
pip install -r requirements.txt
streamlit run app.py
```

Dane zapisują się w `~/.pricemon/pricemon.json` (jak w desktopie). Katalog można
zmienić zmienną `PRICEMON_DATA_DIR`.

## Wdrożenie — Streamlit Community Cloud (darmowe)

1. Wrzuć repozytorium na GitHub.
2. https://share.streamlit.io → *New app* → wskaż repo i `app.py`.
3. Deploy.

**Ograniczenia darmowego hostingu (ważne):**

- **Blokady IP** — Amazon/Allegro/AliExpress (DataDome) często blokują adresy
  serwerowni. `curl_cffi` pomaga, ale skuteczność zależy od puli IP hostingu.
  Część stron może zwracać „ochrona antybotowa”. Dla pełnej niezawodności → VPS.
- **Efemeryczny dysk** — po restarcie/redeployu `pricemon.json` znika. Rób kopię
  przez **eksport JSON** w aplikacji (i importuj po restarcie).
- **Brak tła 24/7** — Streamlit nie sprawdza cen, gdy nikt nie patrzy. Sprawdzanie
  jest na żądanie („Sprawdź wszystko”).

## Wdrożenie — VPS (pełna funkcjonalność + monitoring 24/7)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export PRICEMON_DATA_DIR=/opt/priscanner/data   # trwały wolumen

# UI (np. za reverse-proxy Caddy/Nginx):
streamlit run app.py --server.port 8501 --server.address 127.0.0.1
```

Cykliczne sprawdzanie cen w tle — `cron`:

```cron
0 */4 * * *  cd /opt/priscanner && PRICEMON_DATA_DIR=/opt/priscanner/data \
             /opt/priscanner/.venv/bin/python check_cli.py >> check.log 2>&1
```

`check_cli.py` aktualizuje ten sam `pricemon.json`, więc UI pokazuje świeże ceny.

## Playwright (opcjonalny — render stron z ceną w JS, np. AliExpress)

```bash
pip install playwright
playwright install chromium
```

Na Streamlit Community Cloud: odkomentuj `playwright` w `requirements.txt` oraz
zależności systemowe w `packages.txt`. Bez Playwrighta aplikacja działa w pełni
dla stron oddających cenę w HTML (Amazon, Allegro, Ceneo, większość sklepów);
strony w 100% JS zwrócą wtedy komunikat o potrzebie renderu.

## Różnice względem desktopu

- Menu kontekstowe (PPM) i kafelki najechania → **panel „szczegóły i akcje”** pod
  tabelą (wybór pozycji w liście rozwijanej). Te same akcje: sprawdź ponownie,
  ustaw/zeruj bazę, ulubione, oznacz zakończoną/wznów, monitorowanie Ceneo, szukaj
  w porównywarce, otwórz/kopiuj link, usuń.
- Przeciąganie wierszy → przyciski **↑/↓** w panelu.
- Render w widocznym oknie (headed) → na serwerze bezsensowny; przycisk
  „Renderuj (Playwright)” wymusza render headless, gdy Playwright jest dostępny.
