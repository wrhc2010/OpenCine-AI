FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE /app/
COPY src /app/src
RUN pip install --no-cache-dir ".[api]"
EXPOSE 8000
CMD ["uvicorn", "video_director.api:app", "--host", "0.0.0.0", "--port", "8000"]
