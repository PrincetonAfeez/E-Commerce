FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_ENV=production

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libpq5 postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/docker-entrypoint.sh

RUN DJANGO_ENV=production DJANGO_DEBUG=0 \
    DJANGO_SECRET_KEY=build-time-secret \
    DJANGO_ALLOWED_HOSTS=localhost \
    DJANGO_SITE_URL=http://localhost:8000 \
    DJANGO_EMAIL_HOST=localhost \
    TLS_CHECK_SECRET=build-time-tls \
    OPS_METRICS_SECRET=build-time-metrics \
    MEDIA_PERSIST_LOCAL=1 \
    CACHE_URL=redis://127.0.0.1:6379/0 \
    DATABASE_URL=sqlite:////tmp/build.db \
    python manage.py collectstatic --noinput

RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/healthz/ || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60"]
