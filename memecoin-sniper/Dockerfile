FROM python:3.11-slim
WORKDIR /app
# exact, hash-checked dependencies: a tampered or surprise new release can't slip in
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml ./
COPY sniper ./sniper
RUN pip install --no-cache-dir --no-deps .
# wallet key, settings and trade history live here; mount a volume so they survive upgrades
VOLUME /app/data
CMD ["python", "-m", "sniper", "bot"]
