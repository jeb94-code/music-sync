FROM python:3.12-slim

# Unbuffered stdout so `docker logs` shows progress live rather than in bursts.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY musicsync ./musicsync

# Run as a non-root user. The UID is fixed so the named volume keeps working
# across image rebuilds.
RUN useradd --create-home --home-dir /home/musicsync --uid 10001 \
        --shell /usr/sbin/nologin musicsync \
    && mkdir -p /data \
    && chown -R musicsync:musicsync /data /app
USER musicsync

VOLUME ["/data"]

# The sync loop refreshes /data/heartbeat after every pass.
HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=3 \
    CMD ["python", "-m", "musicsync.healthcheck"]

ENTRYPOINT ["python", "-m", "musicsync"]
