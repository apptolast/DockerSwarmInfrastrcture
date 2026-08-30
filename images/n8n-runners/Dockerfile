FROM docker.io/library/node:26.3.0-alpine3.22@sha256:c7932b9e5e337b0e733d6e16abc1b0e104759e8b05e59ed56586cce967d26dfe AS dependencies

WORKDIR /dependencies
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev \
    && npm cache clean --force

FROM docker.io/n8nio/runners:2.36.8@sha256:11b7406a38f220e23f8c0c1e5d5368b6e837bf65a7a8c8e69782e597835c0896

LABEL org.opencontainers.image.source="https://github.com/apptolast/DockerSwarmInfrastrcture" \
      org.opencontainers.image.description="n8n 2.31.5 task runners with the exact external modules used by the restored workflows"

USER root

COPY --from=dependencies --chown=runner:runner \
  /dependencies/node_modules/ \
  /opt/runners/task-runner-javascript/node_modules/

USER runner
