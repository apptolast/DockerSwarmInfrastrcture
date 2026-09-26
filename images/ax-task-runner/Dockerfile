# Upstream Dockerfile.task-runner (google/ax f009cc8) with every input pinned.
# The build context is staged with the runner binary, compiled beforehand with
# the Makefile's exact flags, and cmd/ax-task-runner/antigravity_bootstrap.py
# from the reviewed, patched checkout (google/ax#364, #375).
FROM docker.io/library/python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

RUN apt-get update && apt-get install -y --no-install-recommends \
    git=1:2.47.3-0+deb13u1 \
    curl=8.14.1-2+deb13u5 \
    ca-certificates=20250419 \
    openssh-client=1:10.0p1-7+deb13u4 \
    procps=2:4.0.4-9 \
    bash=5.2.37-2+b10 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir google-antigravity==0.1.18

COPY bin/linux_amd64/ax-task-runner /usr/local/bin/ax-task-runner
COPY cmd/ax-task-runner/antigravity_bootstrap.py /usr/local/bin/antigravity_bootstrap.py

ENTRYPOINT ["/usr/local/bin/ax-task-runner"]
