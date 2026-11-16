FROM node:22-bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip python3-venv git build-essential ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 forge \
 && useradd --uid 10001 --gid forge --create-home --shell /usr/sbin/nologin forge \
 && mkdir -p /workspace && chown forge:forge /workspace
COPY apps/api/app/sandbox/egress.py /opt/forge/egress_proxy.py
USER 10001:10001
WORKDIR /workspace
CMD ["sleep", "infinity"]
