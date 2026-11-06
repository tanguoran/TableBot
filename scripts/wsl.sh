#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

if [[ ! -f .env ]]; then
    cp .env.example .env
    echo 'Created .env from .env.example.' >&2
fi

case "${1:-run}" in
    run) exec python3 -m jarvis ;;
    check) exec python3 -m jarvis --check ;;
    test) exec python3 -m unittest discover -s tests -v ;;
    smoke) exec python3 -m jarvis --smoke-test ;;
    search-test) exec python3 -m jarvis --search-test ;;
    kb-index) exec python3 -m jarvis --reindex-knowledge ;;
    kb-smoke) exec python3 -m jarvis.knowledge_smoke ;;
    vision-smoke) exec python3 -m jarvis.vision_smoke ;;
    desktop-bridge) exec python3 -m jarvis.desktop_bridge ;;
    desktop-mcp) exec python3 -m jarvis.desktop_mcp ;;
    desktop-smoke) exec python3 -m jarvis.desktop_smoke ;;
    monitor) exec python3 -m jarvis.monitor ;;
    pull)
        model=$(python3 -c 'from jarvis.config import MODEL; print(MODEL)')
        embedding_model=$(python3 -c 'from jarvis.config import EMBED_MODEL; print(EMBED_MODEL)')
        ollama pull "$model"
        exec ollama pull "$embedding_model"
        ;;
    *) echo 'Usage: bash scripts/wsl.sh [run|check|test|smoke|pull|search-test|kb-index|kb-smoke|vision-smoke|desktop-bridge|desktop-mcp|desktop-smoke|monitor]' >&2; exit 2 ;;
esac

