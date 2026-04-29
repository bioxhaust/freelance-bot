FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Persist the SQLite DB outside the container by mounting a host file:
#   docker run --rm -it --env-file .env -v $(pwd)/orders.db:/app/orders.db freelance-bot
VOLUME ["/app"]

CMD ["python", "main.py"]
