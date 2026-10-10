# Server image: the scanner and site generator, run every 3 hours by
# src/scheduler.py (see docker-compose.yml and the README).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ and docs/ are volumes on the server. The committed database moves to
# seed/, where the scheduler copies it from only when a volume has none yet.
# An unprivileged user owns both directories, so the volumes created from
# them are writable without running as root.
RUN mkdir seed && mv data/prices.db seed/prices.db \
 && useradd --create-home --uid 1000 app \
 && chown -R app:app data docs
USER app

CMD ["python", "-m", "src.scheduler"]
