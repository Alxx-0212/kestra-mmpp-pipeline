FROM python:3.11-slim AS builder

ENV VIRTUAL_ENV=/opt/venv
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV"
COPY finpay_topup_pipeline/requirements-finpay-topup.txt /tmp/requirements.txt
RUN "$VIRTUAL_ENV/bin/pip" install --no-cache-dir -r /tmp/requirements.txt

FROM python:3.11-slim AS runtime

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY finpay_topup_pipeline/ /app/finpay_topup_pipeline/
COPY skills/digipos-topup-monitoring/ /app/skills/digipos-topup-monitoring/

CMD ["python", "-m", "finpay_topup_pipeline"]
