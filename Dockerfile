# One small image serves as both the API and the worker; they differ only in
# the command compose runs. This image does NOT contain the PreTeXt toolchain —
# builds happen in the separate pretextbook/pretext-full image that the worker
# spawns. Keeping this image tiny means fast rebuilds during development.
FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

EXPOSE 8000

# Default command = API. The worker service overrides this in compose.yaml.
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]
