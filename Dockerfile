FROM python:3.13-slim

WORKDIR /app
COPY . /app

# Install dependencies
RUN pip install --no-cache-dir .

# Default plan path can be overridden by env; mount your .atp.json at runtime.
ENV ATP_FILE=/data/.atp.json \
    ATP_LEASE_SECONDS=600

ENTRYPOINT ["python3", "/app/main.py"]
