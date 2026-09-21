FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system app \
    && adduser --system --ingroup app --home /app app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY --chown=app:app . /app
RUN mkdir -p /app/data \
    && chown -R app:app /app/data

USER app
WORKDIR /app

EXPOSE 8000

CMD ["python", "-m", "src.main"]
