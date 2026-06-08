FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server_migrator.py .

# Koyeb poda PORT przez zmienna srodowiskowa (domyslnie 8000).
ENV PORT=8000
EXPOSE 8000

# WAZNE: tylko 1 worker gunicorn i 1 proces, bo migracja dziala w watku
# w tle wewnatrz procesu. Wiele workerow = wiele rownoleglych migracji
# (kazdy walnie w Gmaila) - tego nie chcemy.
# --timeout 0 : nie zabijaj dlugo zyjacego procesu.
# --threads 4 : obsluga zapytan web GUI obok watku migracji.
CMD gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 0 server_migrator:app
