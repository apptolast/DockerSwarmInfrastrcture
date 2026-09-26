# Unmodified Claude Code and Codex CLI on top of the reviewed AX task runner
# (docs/runner.md, level 1 customization: the image keeps the runner at
# /usr/local/bin/ax-task-runner and adds the agents).
ARG RUNNER_IMAGE

FROM docker.io/library/node:24-trixie-slim@sha256:8ec5d7557396cfe32d21c3f9c13072355ceab22b584578ca4bb28af31120cffe AS agents

WORKDIR /opt/agents
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-fund --no-audit \
    && npm cache clean --force

FROM ${RUNNER_IMAGE}

COPY --from=agents /usr/local/bin/node /usr/local/bin/node
COPY --from=agents /opt/agents /opt/agents
COPY ax-agent /usr/local/bin/ax-agent
RUN install -d -m 0700 /root/.codex

ENV PATH="/opt/agents/node_modules/.bin:${PATH}" \
    DISABLE_AUTOUPDATER=1 \
    CODEX_HOME=/root/.codex
