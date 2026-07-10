FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV UV_LINK_MODE=copy

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
