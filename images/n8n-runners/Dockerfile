FROM docker.io/library/node:22.19.0-alpine3.22@sha256:d2166de198f26e17e5a442f537754dd616ab069c47cc57b889310a717e0abbf9 AS dependencies

WORKDIR /dependencies
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev \
    && npm cache clean --force

FROM docker.io/n8nio/runners:2.31.5@sha256:ac5ed40759bfe754cc8ab91ed2a1db6795015173084411f4392bc2dc608833b3

LABEL org.opencontainers.image.source="https://github.com/apptolast/DockerSwarmInfrastrcture" \
      org.opencontainers.image.description="n8n 2.31.5 task runners with the exact external modules used by the restored workflows"

USER root

COPY --from=dependencies --chown=runner:runner \
  /dependencies/node_modules/ \
  /opt/runners/task-runner-javascript/node_modules/

USER runner
