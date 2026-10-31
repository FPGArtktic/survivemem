# SPDX-License-Identifier: GPL-3.0-or-later
# survivemem daemon (Profile B): MCP over Streamable HTTP + hook endpoint, one DB owner.
#   podman build -t localhost/survivemem:latest .
FROM docker.io/library/python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra vec --no-editable

FROM docker.io/library/python:3.12-slim
RUN useradd --uid 1000 --create-home survivemem && mkdir /data && chown survivemem /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    SURVIVEMEM_DB=/data/memory.db \
    SURVIVEMEM_CONFIG=/data/config.toml \
    XDG_STATE_HOME=/data/state \
    SURVIVEMEM_IN_CONTAINER=1 \
    SURVIVEMEM_OLLAMA_URL=http://host.containers.internal:11434
USER survivemem
VOLUME /data
EXPOSE 8765
ENTRYPOINT ["survivemem"]
# 0.0.0.0 inside the container only; publish the port to the host's 127.0.0.1.
CMD ["serve", "--http", "--bind", "0.0.0.0:8765"]
