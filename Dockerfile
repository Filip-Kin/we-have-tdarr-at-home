FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        fastapi==0.115.4 \
        'uvicorn[standard]==0.32.0' \
        jinja2==3.1.4 \
        inotify_simple==1.3.5

RUN mkdir -p /data && chown 33:33 /data
VOLUME ["/data"]

WORKDIR /app
COPY app/ /app/

ENV PYTHONUNBUFFERED=1
EXPOSE 8128

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8128", "--log-level", "info"]
