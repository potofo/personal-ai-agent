FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
# curl: used by the Coder agent init script to download the agent binary.
# tini: PID 1 that reaps zombies and forwards signals.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -u 10001 -m agent && mkdir /data && chown agent:agent /data
COPY app/ ./app/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
USER agent
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1
ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
