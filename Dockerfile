FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENABLE_WEB_INTERFACE=true \
    GRADIO_ANALYTICS_ENABLED=false

RUN useradd --create-home --uid 1000 app

WORKDIR /app
COPY requirements-openenv.lock ./
RUN pip install --no-cache-dir -r requirements-openenv.lock
COPY pyproject.toml README.md LICENSE ./
COPY airline_recovery ./airline_recovery
RUN pip install --no-cache-dir --no-deps .

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["python", "-m", "airline_recovery.openenv_adapter.server", "--host", "0.0.0.0", "--port", "8000"]
