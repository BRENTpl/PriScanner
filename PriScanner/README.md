# PriceMon (Streamlit)

Webowa wersja PriceMona oparta na Streamlicie. Wklejasz link do produktu,
backend pobiera stronę, rozpoznaje cenę/nazwę/dostępność, a w tle — co zadany
interwał (domyślnie 4 h) i po starcie serwera — sprawdza zmiany cen. Dla
produktów z Amazona UE porównuje ten sam ASIN między rynkami i przelicza waluty.

## Architektura

```
przeglądarka ──WebSocket──> Streamlit (streamlit_app.py)  ← warstwa UI
                                  │
                                  ▼
                          Engine (engine.py)   ← singleton st.cache_resource
                          ├─ pula wątków (pobieranie)
                          ├─ scheduler w tle (start + co N h)
                          └─ scraper.py ──curl_cffi/requests/Playwright──> sklepy
                                  │
                                  ▼
                          SQLite (storage.py, /app/data)
```

- `scraper.py` — czysta logika rozpoznawania ceny, **przeniesiona 1:1 z desktopu**
  (Amazon/Allegro/AliExpress, fingerprint TLS, render JS w podprocesie). Bez zmian.
- `storage.py` — trwałość w SQLite (+ kolumny `status`/`error` dla UI).
- `engine.py` — pula wątków + scheduler; cała logika `do_fetch`/`do_compare`/`check_all`.
- `streamlit_app.py` — wyłącznie interfejs.

### Dlaczego silnik jest osobnym singletonem

Streamlit przelatuje skrypt od nowa przy każdej interakcji, więc nie ma w nim
naturalnego miejsca na „zadanie chodzące co 4 h, gdy nikt nie patrzy". Rozwiązanie:
`Engine` (pula wątków + wątek schedulera) tworzony raz przez `st.cache_resource` —
żyje przez cały czas życia procesu serwera, niezależnie od rerunów i sesji.
Podgląd „na żywo" (postęp pobierania) realizuje auto-odświeżanie co 5 s, które
odczytuje aktualny stan z bazy (zamiast pushu SSE — Streamlit go nie udostępnia).

## Uruchomienie lokalne

```bash
pip install -r requirements.txt
playwright install chromium          # render JS / AliExpress
export PRICEMON_PASSWORD=cokolwiek    # puste = brak bramki logowania
streamlit run streamlit_app.py
```
Otwórz http://localhost:8501. Jeśli ustawiłeś hasło — pojawi się prosta bramka
logowania (hasło z `PRICEMON_PASSWORD`).

## Wdrożenie na publicznym VPS (Docker + HTTPS)

```bash
cp .env.example .env
nano .env                 # USTAW MOCNE PRICEMON_PASSWORD
nano Caddyfile            # wpisz swoją domenę (rekord A musi wskazywać na VPS)
docker compose up -d --build
```
Caddy automatycznie wyrobi certyfikat Let's Encrypt i będzie proxować ruch
(WebSockety Streamlita obsługuje out-of-the-box). Dane (SQLite) lądują w `./data`.

Bez Caddy: usuń usługę `caddy` z `docker-compose.yml`, odkomentuj
`ports: ["8501:8501"]` w usłudze `pricemon`, postaw własny reverse proxy
(pamiętaj o przepuszczeniu WebSocketów / `Upgrade`).

## Konfiguracja (zmienne środowiskowe)

| Zmienna                   | Domyślnie | Opis                                          |
|---------------------------|-----------|-----------------------------------------------|
| `PRICEMON_PASSWORD`       | *(brak)*  | hasło bramki; **puste = brak autoryzacji**    |
| `PRICEMON_INTERVAL_HOURS` | `4`       | startowy interwał (zmienialny w panelu)       |
| `PRICEMON_WORKERS`        | `3`       | równoległe pobrania                           |
| `PRICEMON_DATA`           | `./data`  | katalog bazy SQLite                           |

## Uwagi i ograniczenia

- **Jeden proces serwera.** Scheduler i pula wątków są stanowe w obrębie procesu;
  nie skaluj przez wiele replik bez wyniesienia kolejki na zewnątrz.
- **Streamlit Community Cloud nie jest dobrym celem** dla tej aplikacji: usypia po
  bezczynności (więc scheduler nie chodzi w tle) i ma kłopot z ciężkim Chromium.
  Dlatego celujemy w self-host na VPS-ie (Docker).
- **Bramka logowania** jest minimalistyczna (hasło w `st.session_state`). Na poważnie
  warto dodatkowo osłonić aplikację autoryzacją na reverse proxy (np. Basic Auth w Caddy).
- **Render w widocznym oknie** (desktopowe „Sprawdź w oknie…") nie istnieje — serwer
  nie ma wyświetlacza. Render headless działa.
- **Blokady antybotowe** zależą od IP serwera; VPS-y bywają oznaczane przez DataDome
  (Allegro/AliExpress) częściej niż łącza domowe.

## Migracja z desktopu

Eksport/import używa formatu `pricemon-list` (v1), zgodnego z aplikacją desktopową —
przeniesiesz listę w obie strony przez panel boczny (Eksport / Import).
