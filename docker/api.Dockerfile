FROM python:3.12-slim
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE NOTICE /app/
COPY src /app/src
COPY docker/backend-entrypoint.sh /app/docker/backend-entrypoint.sh
RUN pip install --no-cache-dir ".[api]"
EXPOSE 8000
RUN chmod +x /app/docker/backend-entrypoint.sh
CMD ["/app/docker/backend-entrypoint.sh"]
