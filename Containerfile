FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim

ARG NVM_VERSION=0.40.5
ARG NODE_VERSION=22
ARG OPENCODE_VERSION=1.17.13
ARG INSTALL_OPENCODE=true

ENV DEBIAN_FRONTEND=noninteractive
ENV NVM_DIR=/usr/local/nvm
ENV UV_LINK_MODE=copy
ENV PATH=/usr/local/node-current/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
        openssh-client \
        ripgrep \
    && git config --system init.defaultBranch main \
    && rm -rf /var/lib/apt/lists/*

RUN bash -lc 'mkdir -p "$NVM_DIR" \
    && curl -fsSL "https://raw.githubusercontent.com/nvm-sh/nvm/v${NVM_VERSION}/install.sh" | bash \
    && source "$NVM_DIR/nvm.sh" \
    && nvm install "$NODE_VERSION" \
    && nvm alias default "$NODE_VERSION" \
    && nvm use default \
    && npm install -g npm@latest \
    && if [ "$INSTALL_OPENCODE" = "true" ]; then npm install -g "opencode-ai@${OPENCODE_VERSION}"; fi \
    && ln -sfn "$NVM_DIR/versions/node/$(nvm version default)" /usr/local/node-current \
    && nvm cache clear'

WORKDIR /workspace

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --dev --no-install-project

COPY . .
RUN uv sync --locked --dev

CMD ["bash"]
