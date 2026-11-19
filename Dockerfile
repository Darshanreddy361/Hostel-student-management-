
FROM python:3.12-slim

WORKDIR /app

ENV DATABASE_PATH=/app/data/hostel.db \
    RECEIPT_UPLOAD_DIR=/app/data/receipts

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["python", "app.py"]
