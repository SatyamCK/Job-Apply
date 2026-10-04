FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY packages/common /app/packages/common
COPY services/api /app/services/api
RUN pip install /app/packages/common /app/services/api

COPY migrations /app/migrations
COPY scripts /app/scripts
COPY config/schema.json /app/config/schema.json

RUN useradd --system --uid 10001 app
USER app

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
