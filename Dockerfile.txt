FROM python:3.11-slim

WORKDIR /app

# Сначала только requirements — чтобы кэшировалось
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Потом остальные файлы
COPY . .

EXPOSE 8000

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
