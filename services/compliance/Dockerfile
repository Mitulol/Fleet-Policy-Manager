FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY services/compliance/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/fleetcommon ./fleetcommon
COPY services/compliance/app ./app

EXPOSE 8003

# One uvicorn worker per container, deliberately. Scaling happens by adding
# replicas behind the load balancer, which keeps one instance id equal to one
# replica and makes load distribution unambiguous to read.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8003/health',timeout=3).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"]
