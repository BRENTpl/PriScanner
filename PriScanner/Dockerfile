# PriceMon (Streamlit) — obraz z Chromium dla Playwrighta.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PRICEMON_DATA=/app/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium

COPY scraper.py storage.py engine.py streamlit_app.py ./
COPY .streamlit/ ./.streamlit/

RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8501

# Streamlit trzyma silnik (scheduler + pula wątków) jako singleton w procesie,
# więc jeden proces serwera.
CMD ["streamlit", "run", "streamlit_app.py"]
