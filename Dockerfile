FROM python:3.11-slim

WORKDIR /app

# System deps for polars (needs Rust libs on some slim images)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the pipeline module into the image
COPY pipeline_refactored.py ./pipeline.py

CMD ["python", "-c", "print('finpay-pipeline image ready')"]
