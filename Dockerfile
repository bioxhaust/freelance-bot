FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Mount only the database file, not /app. A volume on /app hides code
# updates: docker keeps the first copy of the sources and ignores rebuilds.
#   docker run --env-file .env -v $(pwd)/orders.db:/app/orders.db freelance-bot

CMD ["python", "main.py"]
