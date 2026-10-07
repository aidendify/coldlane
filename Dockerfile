FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DATABASE_PATH=/data/coldlane.db

RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The whole package: app, worker, templates, static files and Verifier scripts.
COPY coldlane/ ./coldlane/
COPY sample-leads.csv ./sample-leads.csv

RUN mkdir -p /data && chown -R appuser:appuser /data /app
USER appuser
VOLUME ["/data"]
EXPOSE 8080

CMD ["sh", "-c", "exec gunicorn --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT:-8080} --access-logfile - 'coldlane.web:create_app()'"]
