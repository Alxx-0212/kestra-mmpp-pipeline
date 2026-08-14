FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY linkaja_fee_pipeline/dashboard/requirements.txt /tmp/dashboard-requirements.txt
RUN pip install --no-cache-dir -r /tmp/dashboard-requirements.txt

COPY linkaja_fee_pipeline /app/linkaja_fee_pipeline

EXPOSE 5000

CMD ["python", "-m", "linkaja_fee_pipeline.dashboard"]
