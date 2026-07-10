web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
# Railway ignores extra Procfile process types; run the spread bot as a
# second Railway service on this repo with this start command (see README).
worker: python -m app.spreads
