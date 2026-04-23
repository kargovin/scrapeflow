FROM python:3.12-slim

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml .
RUN pip install --no-cache-dir .

FROM python:3.12-slim

COPY --from=0 /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY . .

RUN useradd --no-create-home --uid 1001 appuser
USER appuser

CMD ["python", "server.py"]
