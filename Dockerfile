FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    ASTOCK_CONFIG=/app/config.yaml

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY config.example.yaml ./config.example.yaml
RUN mkdir -p /app/data

CMD ["python", "-m", "astock_bot.main", "run"]

