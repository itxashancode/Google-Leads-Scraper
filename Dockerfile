FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    wget gnupg curl libnss3 libatk-bridge2.0-0 \
    libxkbcommon0 libgtk-3-0 libasound2 && \
    rm -rf /var/lib/apt/lists/*

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

RUN python -m playwright install --with-deps chromium

COPY . .

CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}
