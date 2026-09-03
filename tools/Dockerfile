# Lets the fleet simulator run with no Python setup on the host at all:
#   docker compose run --rm simulator --devices 500 --duration 120
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /srv

COPY tools/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY tools/simulator.py ./simulator.py
COPY tools/seed_policies.py ./seed_policies.py

ENTRYPOINT ["python", "simulator.py"]
