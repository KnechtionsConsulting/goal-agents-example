# The Kelos Claude Code image plus python3, which the agents need for tools/goals/.
# Pin the tag to the Kelos release you run.
FROM ghcr.io/kelos-dev/claude-code:v0.64.0
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
USER claude
