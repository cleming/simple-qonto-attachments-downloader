FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY download_receipts.py .

RUN mkdir -p /app/receipts_sync

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "download_receipts.py"]
CMD ["--days", "90"]
