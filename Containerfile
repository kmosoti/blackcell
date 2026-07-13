FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim AS uv-base

ENV DEBIAN_FRONTEND=noninteractive
ENV UV_LINK_MODE=copy

FROM uv-base AS development

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        git \
        ripgrep \
    && git config --system init.defaultBranch main \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --all-groups --no-install-project

COPY . .
RUN uv sync --locked --all-groups

CMD ["bash"]

FROM uv-base AS runtime-builder

ENV UV_COMPILE_BYTECODE=1

WORKDIR /opt/blackcell

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim-trixie AS runtime

ENV HOME=/home/blackcell \
    PATH=/opt/blackcell/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GIT_OPTIONAL_LOCKS=0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && groupadd --gid 10001 blackcell \
    && useradd --no-log-init --uid 10001 --gid 10001 \
        --home-dir /home/blackcell --create-home --shell /usr/sbin/nologin blackcell \
    && install -d -o blackcell -g blackcell -m 0700 \
        /opt/blackcell /var/lib/blackcell /workspace/repository \
    && git config --system init.defaultBranch main \
    && git config --system --add safe.directory /workspace/repository \
    && rm -rf /var/lib/apt/lists/*

COPY --from=runtime-builder --chown=10001:10001 /opt/blackcell/.venv /opt/blackcell/.venv

WORKDIR /opt/blackcell

USER 10001:10001

EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import os, urllib.request; response = urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('BLACKCELL_BIND_PORT', '8080')}/health/ready\", timeout=3); response.close()"]

ENTRYPOINT ["blackcell-runtime"]
CMD ["api"]
