# Couche minimale au-dessus de l'image officielle TabbyAPI, épinglée par
# digest (voir docs/engineering-log.md, "Triplet de versions" pour le
# triplet exllamav3/torch/tabbyAPI constaté à chaque bump). Nécessaire :
# l'image officielle contient gcc mais PAS les en-têtes de développement
# Python (python3-dev), requis par la compilation JIT Triton du module
# gated_delta_net (attention hybride/SSM de Qwen3.5/3.6/3.8) au premier
# chargement du modèle — sans ce correctif, le chargement échoue
# systématiquement avec "fatal error: Python.h: No such file or
# directory".
#
# Bumped 2026-09-16 (docs/briefs/qwen3.8-27b-evaluation.md, Phase 0):
# exllamav3 1.1.0 -> 1.5.0, required for Qwen3.8's EXL3 codebook version
# (1.4.x+ line) to decode correctly rather than silently mis-decode.
# Verified in isolation first (poc/qwen3.8/, docs/engineering-log.md,
# "Qwen3.8-27B evaluation, Phase 0") before this bump — this Dockerfile
# change itself still needs its own regression smoke on the CURRENT
# Qwen3.6 production model before Qwen3.8 is loaded anywhere near
# production (scripts/smoke-tabbyapi-image-upgrade.sh).
FROM ghcr.io/theroyallab/tabbyapi@sha256:10bfcf9d27d1b3a5c7ada786f814b9da43fadad35643362f2fe0816082893172

RUN apt-get update && apt-get install -y --no-install-recommends python3-dev \
    && rm -rf /var/lib/apt/lists/*
