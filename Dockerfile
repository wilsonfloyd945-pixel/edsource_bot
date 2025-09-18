FROM python:3.13-slim

WORKDIR /app

# Системные зависимости по минимуму (при необходимости можно добавить)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && rm -rf /var/lib/apt/lists/*

# Устанавливаем Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Кладём исходники
COPY . .

# ВАЖНО: слушаем порт 80 (для Amvera)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]
