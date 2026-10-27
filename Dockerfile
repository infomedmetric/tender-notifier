FROM mcr.microsoft.com/playwright/python:v1.64.1-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=10000
EXPOSE 10000

# Use gunicorn if available; fallback to python
CMD gunicorn tender_notifier:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4 --timeout 120
