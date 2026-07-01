# OpenWeb Cost Agents

This repository contains a local Open WebUI stack, a dedicated Open WebUI
Pipeline adapter, and a Google ADK multi-agent runtime for cloud cost analysis.

The current app is read-only and cost-focused:

- **Open WebUI** as the UI.
- **Open WebUI Pipelines** as the adapter between Open WebUI and ADK.
- **Google ADK** as the agent runtime.
- **LiteLLM** as the provider adapter for Amazon Bedrock Claude or Vertex AI
  Gemini.
- Native read-only tools for AWS Cost Explorer and Hetzner Cloud.

```text
User
 |
 v
Open WebUI -- OpenAI-compatible --> Pipelines
                                      |
                                      | HTTP/SSE (/run_sse)
                                      v
                               ADK api_server
                                      |
                                      v
                           platform_orchestrator
                                cost router
                                /         \
                               v           v
                     aws_cost_agent   hetzner_cost_agent
```

## Main Entry Points

- [docker-compose.yaml](docker-compose.yaml) - local Open WebUI, Pipelines, ADK
  API server, and Postgres stack.
- [DESIGN_SPEC.md](DESIGN_SPEC.md) - current v2 architecture contract.
- [adk/](adk/) - Google ADK app, shared runtime context, Dockerfile, and Python
  requirements.
- [openweb-pipe/agent-pipeline.py](openweb-pipe/agent-pipeline.py) - Open WebUI
  pipeline for `platform_agent`.
- [.env.example](.env.example) - root Compose environment reference.
- [adk/.env.example](adk/.env.example) - ADK-local environment reference.

## Layout

```text
adk/
|-- requirements.txt
|-- common/
|   |-- __init__.py
|   `-- runtime_context.py
`-- platform_agent/
    |-- __init__.py
    |-- agent.py
    `-- sub_agents/
        |-- aws_cost_agent/
        `-- hetzner_cost_agent/
```

The ADK app directory exports `root_agent` from
`adk/platform_agent/agent.py`.

## Agent Design

The initial v2 app is read-only and cost-focused:

- `platform_orchestrator` routes requests and coordinates both specialists
  when a question spans providers.
- `aws_cost_agent` queries AWS Cost Explorer through `boto3`.
- `hetzner_cost_agent` queries Hetzner Cloud inventory and pricing through
  `httpx`.

Sub-agent collaboration is orchestrator-mediated. A specialist can recommend
consulting the other specialist, and the orchestrator performs the next
delegation and combines results. Free-form peer-to-peer loops are intentionally
out of scope for this starter version.

## Environment Variables

Configure the root `.env` consumed by `docker-compose.yaml`.

Model backend:

| Variable | Description |
|---|---|
| `LLM_PROVIDER` | `bedrock` by default. Set `vertex_ai` to use Gemini. |
| `ORCHESTRATOR_MODEL` | Optional provider-specific override. Bedrock expects a Bedrock model ID; Vertex expects a Gemini model name. Blank uses provider defaults. |
| `SUB_AGENT_MODEL` | Optional provider-specific override for sub-agents. Blank uses provider defaults. |
| `BEDROCK_MODEL_ID` | Compatibility fallback for generic Bedrock callers. Default: `eu.anthropic.claude-sonnet-4-6`. |
| `AWS_BEARER_TOKEN_BEDROCK` | Optional Bedrock API key. Otherwise LiteLLM uses standard AWS credentials. |
| `AWS_REGION_NAME` | AWS region for Bedrock and boto3 clients. Default: `eu-central-1`. |
| `AWS_DEFAULT_REGION` | Fallback AWS region used by AWS SDKs. |
| `GEMINI_MODEL` | Gemini fallback model when `LLM_PROVIDER=vertex_ai`. |
| `FALLBACK_REGIONS` | Comma-separated Vertex regions for Gemini fallback. |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to GCP service account JSON inside the container. |
| `GOOGLE_CLOUD_PROJECT` | GCP project ID. |
| `GOOGLE_CLOUD_LOCATION` | Primary Vertex region. |
| `GOOGLE_GENAI_USE_VERTEXAI` | `true` to force Vertex backend in the Google SDK. |

Default role models:

- Bedrock orchestrator: `eu.anthropic.claude-sonnet-4-6`
- Bedrock sub-agents: `eu.anthropic.claude-haiku-4-5-20251001-v1:0`
- Vertex mode: both roles fall back to `GEMINI_MODEL` unless role overrides
  are set.

Cost integrations:

| Variable | Description |
|---|---|
| `AWS_PROFILE` | AWS profile used by boto3 for Cost Explorer / Bedrock when not using a bearer token. |
| `AWS_ACCOUNT_ALIASES` | Comma-separated `account_id=alias` mapping for AWS cost output. |
| `HETZNER_TOKEN` | Hetzner Cloud API token. Read-only scope is sufficient for cost analysis. |
| `AGENT_REPORTS_DIR` | Directory where AWS CSV reports are written. Compose mounts `/app/reports`. |

Runtime/session behavior:

| Variable | Description |
|---|---|
| `ADK_SESSION_DB_URL` | ADK session service URI. Compose builds this from Postgres vars. |
| `LITELLM_LOG` | LiteLLM log level. |
| `LLM_CALL_DELAY` | Optional seconds between LLM calls. |
| `LLM_RETRY_MAX_ATTEMPTS` | Retry attempts for transient LLM failures. |
| `LLM_RETRY_INITIAL_DELAY` | Initial retry delay in seconds. |
| `LLM_RETRY_MAX_DELAY` | Max retry delay in seconds. |
| `LLM_RETRY_BACKOFF_FACTOR` | Retry backoff multiplier. |
| `ENABLE_HISTORY_COMPACTION` | Enables prompt history trimming before model calls. |
| `MAX_LLM_HISTORY_CONTENTS` | Max ADK content items retained in a model request. |
| `MAX_LLM_HISTORY_CHARS` | Max estimated prompt history characters retained. |

## Local Run

The full local stack is managed through Compose:

```bash
docker compose up -d
```

Open WebUI is available at `http://localhost:3000`. The ADK API server is
available at `http://localhost:8000`, and the Open WebUI Pipelines service is
available at `http://localhost:9099`.

For ADK-only local development:

```bash
cd adk
pip install -r requirements.txt

# Bedrock default
export LLM_PROVIDER=bedrock
export AWS_REGION_NAME=eu-central-1
export AWS_PROFILE=your-profile

# Cost integrations
export AWS_ACCOUNT_ALIASES="111111111111=personal-root"
export HETZNER_TOKEN=...

adk web
```

For Gemini / Vertex AI:

```bash
export LLM_PROVIDER=vertex_ai
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
export GOOGLE_CLOUD_PROJECT=my-gcp-project
export GOOGLE_CLOUD_LOCATION=europe-west4
export GOOGLE_GENAI_USE_VERTEXAI=true
export GEMINI_MODEL=gemini-2.5-flash
```

`adk api_server` exposes:

- `POST /apps/{app}/users/{user}/sessions/{session}`
- `GET /apps/{app}/users/{user}/sessions/{session}`
- `POST /run_sse`
- `GET /list-apps`

The Open WebUI Pipeline uses these endpoints.

## Open WebUI Pipeline

The local pipeline is mounted into the Pipelines container by
`docker-compose.yaml`:

```yaml
- ./openweb-pipe/agent-pipeline.py:/app/pipelines/agent-pipeline.py:ro
```

Its default ADK endpoint is `http://platform-agent:8000`, which is the Compose
service name for the ADK API server.

## AWS Cost Agent

Native tools use `boto3` against AWS Cost Explorer. The agent is read-only.

Tools:

- `get_cost_summary(start_date, end_date, granularity, group_by)`
- `compare_periods(period_a_start, period_a_end, period_b_start, period_b_end, group_by, top_n)`
- `get_top_cost_drivers(start_date, end_date, top_n, group_by)`
- `forecast_costs(start_date, end_date, granularity)`
- `export_cost_report_csv(start_date, end_date, granularity, group_by, filename)`

Required IAM permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "ce:GetCostAndUsage",
      "ce:GetCostForecast",
      "ce:GetDimensionValues",
      "ce:GetTags"
    ],
    "Resource": "*"
  }]
}
```

`AWS_ACCOUNT_ALIASES` can map account IDs to readable names:

```text
AWS_ACCOUNT_ALIASES=111111111111=personal-root,222222222222=personal-prod
```

## Hetzner Cost Agent

Native tools use `httpx` against the Hetzner Cloud API. The agent is read-only
and scoped to one project by `HETZNER_TOKEN`.

Tools:

- `get_hetzner_cost_summary(resource_types, label_filter)`
- `list_hetzner_resources(resource_type, label_filter, sort_by_cost, limit)`
- `get_hetzner_top_cost_drivers(top_n, resource_types, label_filter)`
- `get_hetzner_pricing(resource_type, name_filter, location)`

Hetzner cost is reported as steady-state monthly price, in EUR net
VAT-excluded. It is not historical/prorated billing.

## Dependencies

Important direct runtime dependencies:

- `google-adk` - ADK runtime and API server.
- `litellm` - Bedrock / Vertex model adapter.
- `boto3` - AWS Cost Explorer.
- `httpx` - Hetzner Cloud API.
- `asyncpg` and `sqlalchemy` - Postgres-backed ADK sessions.
- `google-cloud-aiplatform` and `google-auth` - Vertex AI auth.

`adk/requirements-lock.txt` captures a previously validated package set.
Refresh it only as part of a deliberate dependency update.

## Security Notes

- Keep `.env` and cloud credentials out of git.
- Mount AWS credentials and GCP service account files at runtime.
- Use read-only IAM/API tokens for cost analysis whenever possible.
- This starter version has no deployment, reboot, Kubernetes mutation, or
  GitLab pipeline trigger capability.
