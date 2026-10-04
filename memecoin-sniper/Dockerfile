FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
COPY sniper ./sniper
RUN pip install --no-cache-dir .
# wallet key, settings and trade history live here; mount a volume so they survive upgrades
VOLUME /app/data
CMD ["python", "-m", "sniper", "bot"]
