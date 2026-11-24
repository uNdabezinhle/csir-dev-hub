# Build context = repo root (see docker-compose.yml); .dockerignore excludes secrets/
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY stack/portal/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY stack/portal/app ./app
COPY stack/portal/config ./config
COPY identity/roles.json ./config/roles.json
VOLUME /data
EXPOSE 8088
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8088", "--proxy-headers"]
