FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Install Python dependencies first (layer-cached until pyproject.toml changes)
COPY pyproject.toml .
RUN pip install -e .

COPY worker/ ./worker/
COPY tests/ ./tests/

CMD ["python", "-m", "worker.main"]
