FROM docker.io/library/node:26.3.0-alpine3.22@sha256:c7932b9e5e337b0e733d6e16abc1b0e104759e8b05e59ed56586cce967d26dfe AS dependencies

WORKDIR /dependencies
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev \
    && npm cache clean --force

FROM docker.io/n8nio/runners:2.38.7@sha256:82167390e7c3b58ccb30147c9f642997b3074233f1d4bf44fdc1d8a59266b27f

LABEL org.opencontainers.image.source="https://github.com/apptolast/DockerSwarmInfrastrcture" \
      org.opencontainers.image.description="n8n 2.31.5 task runners with the exact external modules used by the restored workflows"

USER root

COPY --from=dependencies --chown=runner:runner \
  /dependencies/node_modules/ \
  /opt/runners/task-runner-javascript/node_modules/

USER runner
