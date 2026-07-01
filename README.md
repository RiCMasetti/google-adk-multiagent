# OpenWeb ADK

This repository contains the local Open WebUI stack, dedicated Open WebUI Pipeline adapters, and a multi-app Google ADK runtime.

The current ADK apps are:

- `platform_agent` — SRE/DevOps app with GitLab, Datadog, AWS cost, Hetzner, and Kubernetes agents.
- `tina_agent` — contract-analysis app backed by the Tina MCP/RAG domain.

The detailed setup, environment variable reference, local run instructions, model configuration, and app descriptions live in [adk/README.md](adk/README.md).

Main entry points:

- [docker-compose.yaml](docker-compose.yaml) — local Open WebUI, Pipelines, ADK API server, and Postgres stack.
- [adk/](adk/) — Google ADK apps, shared runtime context, Dockerfile, and Python requirements.
- [openweb-pipe/agent-pipeline.py](openweb-pipe/agent-pipeline.py) — Open WebUI pipeline for `platform_agent`.
- [openweb-pipe/tina-agent-pipeline.py](openweb-pipe/tina-agent-pipeline.py) — Open WebUI pipeline for `tina_agent`.
