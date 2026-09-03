FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY services/gateway/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/fleetcommon ./fleetcommon
COPY services/gateway/app ./app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"

# Several worker processes, because every request in the fleet passes through
# here and one Python event loop becomes the platform's ceiling under load.
# This is safe only because the gateway keeps no per-process state: rate limit
# buckets live in Redis, so the limit is shared across workers rather than
# being silently multiplied by the worker count.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
