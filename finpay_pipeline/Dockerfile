FROM python:3.11-slim AS builder

ENV VIRTUAL_ENV=/opt/venv
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV"
COPY finpay_pipeline/requirements.txt ./requirements.txt
RUN "$VIRTUAL_ENV/bin/pip" install --no-cache-dir -r requirements.txt

FROM python:3.11-slim AS runtime

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY finpay_pipeline ./finpay_pipeline

CMD ["python", "-c", "print('finpay-pipeline image ready')"]
