# Same patch as services/tabbyapi/Dockerfile, same reason (python3-dev
# needed for gated_delta_net's Triton JIT compile — see that file's
# comment for the full rationale), applied here on top of the digest
# resolved live by scripts/probe-qwen38-runtime-load.sh instead of the
# production one: this PoC targets a different tabbyAPI release on
# purpose (docs/briefs/qwen3.8-27b-evaluation.md, Phase 0).
FROM ghcr.io/theroyallab/tabbyapi@sha256:10bfcf9d27d1b3a5c7ada786f814b9da43fadad35643362f2fe0816082893172

RUN apt-get update && apt-get install -y --no-install-recommends python3-dev \
    && rm -rf /var/lib/apt/lists/*
