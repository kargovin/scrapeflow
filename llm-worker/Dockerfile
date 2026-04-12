FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install -e .

COPY worker/ ./worker/
COPY tests/ ./tests/

CMD ["python", "-m", "worker.main"]
