# OpenWeb ADK Agents — Open WebUI + ADK

ADK runtime for multiple Open WebUI-facing agent apps:

- **Open WebUI** as the UI (Google Workspace auth, history, markdown/code rendering)
- **Open WebUI Pipelines** as dedicated adapters between Open WebUI and ADK apps
- **Google ADK** as the agent runtime (one root orchestrator per app + specialised sub-agents)
- **LiteLLM** as the translator toward Vertex AI Gemini or Amazon Bedrock Claude
- Native tools (GitLab, AWS, Hetzner, Kubernetes) and external MCP servers (Datadog, Tina)

```
User
 │ Google Workspace OIDC
 ▼
Open WebUI ── OpenAI-compat ──► Pipelines
                                       │
                                       │ HTTP/SSE (/run_sse)
                                       ▼
                                ADK api_server
                                       │
                         ┌─────────────┴─────────────┐
                         ▼                           ▼
                  platform_agent                 tina_agent
                 SRE/DevOps app              contract RAG app
                         │                           │
          ┌──────────────┼──────────────┐            ▼
          ▼              ▼              ▼      Tina HTTP MCP
     native tools    Datadog MCP   K8s clients
```

Anything that requires reasoning talks to the configured LiteLLM backend (omitted from the diagram for clarity).

## Layout

```
adk/
├── requirements.txt
├── common/
│   ├── __init__.py
│   └── runtime_context.py             # shared model/date/history helpers
├── platform_agent/
│   ├── __init__.py
│   ├── agent.py                       # platform orchestrator (root_agent)
│   └── sub_agents/
│       ├── gitlab_agent/
│       ├── datadog_agent/
│       ├── aws_cost_agent/
│       ├── hetzner_cost_agent/
│       ├── hetzner_action_agent/
│       ├── k8s_analysis_agent/
│       └── k8s_action_agent/
└── tina_agent/
    ├── __init__.py
    ├── agent.py                       # Tina orchestrator (root_agent)
    └── sub_agents/
        └── contract_analyzer/
            ├── agent.py               # Tina MCP-backed contract agent
            └── cognito_auth.py        # Cognito token helper
```

Each app directory exports `root_agent` from `agent.py`: that's what ADK
looks for when loading an app.

## Environment variables

For the ADK runtime, configure the root `.env` consumed by `docker-compose.yaml`.

Model and Google/AWS backend:

| Variable                          | Description                                                    |
|-----------------------------------|----------------------------------------------------------------|
| `LLM_PROVIDER`                    | `vertex_ai` (default) or `bedrock`                              |
| `GEMINI_MODEL`                    | Gemini model name for Vertex AI mode (default in Compose: `gemini-2.5-flash`) |
| `ORCHESTRATOR_MODEL`              | Optional provider-specific model override for root orchestrators. Falls back to `GEMINI_MODEL` or `BEDROCK_MODEL_ID`. |
| `SUB_AGENT_MODEL`                 | Optional provider-specific model override shared by all sub-agents. Falls back to `GEMINI_MODEL` or `BEDROCK_MODEL_ID`. |
| `BEDROCK_MODEL_ID`                | Bedrock model or inference profile ID for Bedrock mode          |
| `AWS_BEARER_TOKEN_BEDROCK`        | Optional Bedrock API key; otherwise standard AWS credentials are used |
| `AWS_REGION_NAME`                 | AWS region for Bedrock and boto3 clients (default in Compose: `eu-central-1`) |
| `AWS_DEFAULT_REGION`              | Fallback AWS region used by AWS SDKs                            |
| `FALLBACK_REGIONS`                | Comma-separated Vertex regions for 429 fallback                 |
| `GOOGLE_APPLICATION_CREDENTIALS`  | Path to GCP service account JSON                               |
| `GOOGLE_CLOUD_PROJECT`            | GCP project ID                                                 |
| `GOOGLE_CLOUD_LOCATION`           | Primary Vertex region (e.g. `europe-west4`)                    |
| `GOOGLE_GENAI_USE_VERTEXAI`       | `true` to force Vertex backend in google-genai SDK             |

Platform app integrations:

| Variable                          | Description                                                    |
|-----------------------------------|----------------------------------------------------------------|
| `GITLAB_URL`                      | GitLab instance root URL (e.g. `https://gitlab.com`)           |
| `GITLAB_TOKEN`                    | Personal Access Token with `api` scope                         |
| `GITLAB_REPO_URL`                 | Optional authenticated repo URL used by GitLab workflows        |
| `DD_API_KEY`                      | Datadog organisation API key                                   |
| `DD_APPLICATION_KEY`              | Datadog application key (with read scopes for logs/apm/metrics)|
| `DD_MCP_URL`                      | Optional override for the Datadog MCP endpoint. Defaults to EU site. |
| `AWS_PROFILE`                     | AWS profile used by boto3 for Cost Explorer / Bedrock when not using bearer token |
| `AWS_ACCOUNT_ALIASES`             | Comma-separated `account_id=alias` mapping for AWS cost output |
| `HETZNER_TOKEN`                   | Hetzner Cloud API token                                        |
| `KUBE_CLUSTERS`                   | Comma-separated `cluster=/path/to/kubeconfig` registry          |
| `KUBE_MANAGEMENT_CLUSTERS`        | Comma-separated management cluster names refused by mutating K8s actions |
| `ACTION_LOG_DIR`                  | Directory for mutating action audit JSONL logs                  |
| `REPORTS_DIR`                     | Directory for generated reports such as CSV cost exports        |
| `SSH_USER`                        | SSH user for legacy/auxiliary workflows                         |
| `SSH_PRIVATE_KEY_PATH`            | Mounted SSH private key path                                    |
| `SSH_KNOWN_HOSTS_PATH`            | Mounted known-hosts file path                                   |
| `SHELL_TIMEOUT`                   | Timeout for shell-backed operations                             |
| `AGENT_BASE_DIR`                  | Base directory inside the ADK container                         |
| `ANSIBLE_PROJECT_PATH`            | GitLab project path for Ansible jobs                            |
| `ANSIBLE_REPO_REF`                | Git ref used when reading the Ansible job catalog                |
| `ANSIBLE_JOB_CATALOG_PATH`        | Path to the Ansible job catalog in the repo                     |

Tina app integrations:

| Variable                          | Description                                                    |
|-----------------------------------|----------------------------------------------------------------|
| `TINA_MCP_URL`                    | Optional override for Tina MCP. If unset, derived from `TINA_MCP_ENV` and `TINA_MCP_INTERNAL`. |
| `TINA_MCP_ENV`                    | `canary` (default) or `live` for the Tina MCP endpoint.         |
| `TINA_MCP_INTERNAL`               | `true` to use in-cluster Tina MCP service DNS names.            |
| `TINA_MCP_TIMEOUT_SECONDS`        | Timeout for Tina MCP Streamable HTTP connect/initialize calls (default `30`). |
| `TINA_MCP_SSE_READ_TIMEOUT_SECONDS` | SSE read timeout for Tina MCP sessions (default `300`).       |
| `TINA_MCP_BEARER_TOKEN`           | Optional direct bearer token for Tina MCP. If unset, Cognito auth is used. |
| `TINA_COGNITO_AUTH_FLOW`          | Cognito flow. `USER_PASSWORD_AUTH` default; `ADMIN_USER_PASSWORD_AUTH` and `CLIENT_CREDENTIALS` also supported. |
| `TINA_COGNITO_CLIENT_ID`          | Cognito app client ID for Tina MCP authentication.              |
| `TINA_COGNITO_CLIENT_SECRET`      | Cognito app client secret, when configured on the app client.   |
| `TINA_COGNITO_USER_POOL_ID`       | Cognito user pool ID; required for admin auth flows.            |
| `TINA_COGNITO_REGION`             | AWS region for Cognito IDP; falls back to `AWS_REGION_NAME`.    |
| `TINA_COGNITO_USERNAME`           | Static Cognito username for user-password auth flows.           |
| `TINA_COGNITO_PASSWORD`           | Static Cognito password for user-password auth flows.           |
| `TINA_COGNITO_AUTH_ENDPOINT`      | Optional Cognito IDP endpoint override for boto3.               |
| `TINA_COGNITO_TOKEN_ENDPOINT`     | OAuth token endpoint; required for `CLIENT_CREDENTIALS`.        |
| `TINA_COGNITO_JWKS_URL`           | JWKS URL for deployment/config visibility; the agent does not validate tokens locally. |
| `TINA_COGNITO_TOKEN_TYPE`         | `access_token` (default) or `id_token` for boto3 auth responses. |
| `TINA_COGNITO_SCOPES`             | Optional OAuth scopes for `CLIENT_CREDENTIALS`; use a space-delimited string. |
| `TINA_COGNITO_TOKEN_REFRESH_MARGIN_SECONDS` | Seconds before Cognito token expiry to refresh. Defaults to `300`. |

Runtime/session behavior:

| Variable                          | Description                                                    |
|-----------------------------------|----------------------------------------------------------------|
| `ADK_SESSION_DB_URL`              | ADK session service URI; Compose uses Postgres via `postgresql+asyncpg://...` |
| `LITELLM_LOG`                     | LiteLLM log level                                               |
| `LLM_CALL_DELAY`                  | Optional seconds between LLM calls                              |
| `LLM_RETRY_MAX_ATTEMPTS`          | Retry attempts for transient LLM failures                       |
| `LLM_RETRY_INITIAL_DELAY`         | Initial retry delay in seconds                                  |
| `LLM_RETRY_MAX_DELAY`             | Max retry delay in seconds                                      |
| `LLM_RETRY_BACKOFF_FACTOR`        | Retry backoff multiplier                                        |
| `ENABLE_HISTORY_COMPACTION`       | Enables prompt history trimming before model calls (default `true`) |
| `MAX_LLM_HISTORY_CONTENTS`        | Max ADK content items retained in a model request (default `32`) |
| `MAX_LLM_HISTORY_CHARS`           | Max estimated prompt history characters retained (default `120000`) |

For Pipelines (configurable via Valves in the Open WebUI admin UI):

| Valve                         | Description                                       |
|-------------------------------|---------------------------------------------------|
| `ADK_BASE_URL`                | URL of the ADK service (e.g. `http://platform-agent:8000`) |
| `ADK_APP_NAME`                | ADK app directory name, e.g. `platform_agent` or `tina_agent` |
| `REQUEST_TIMEOUT_SECONDS`     | Default 600. High because tools can be long-running. |
| `SHOW_TOOL_ACTIVITY`          | Show "🔧 invoking ..." chips in chat              |

Dedicated pipeline files live outside this directory:

- `openweb-pipe/agent-pipeline.py` exposes `platform_agent`.
- `openweb-pipe/tina-agent-pipeline.py` exposes `tina_agent`.

For local Docker Compose, both pipeline valves should use
`ADK_BASE_URL=http://platform-agent:8000`. Kubernetes
`*.svc.cluster.local` names only resolve from inside the Kubernetes cluster.

## Local run (development)

```bash
cd adk
pip install -r requirements.txt

# For parity with the Docker image, use the checked-in constraints lock:
pip install -r requirements.txt -c requirements-lock.txt

export GITLAB_URL=https://gitlab.com
export GITLAB_TOKEN=glpat-xxx
export DD_API_KEY=...
export DD_APPLICATION_KEY=...
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
export GOOGLE_CLOUD_PROJECT=my-gcp-project
export GOOGLE_CLOUD_LOCATION=europe-west4
export GOOGLE_GENAI_USE_VERTEXAI=true
export LLM_PROVIDER=vertex_ai
export GEMINI_MODEL=gemini-2.5-flash
# Optional: use a stronger/cheaper split by role.
# For vertex_ai these are Gemini model names; for bedrock these are Bedrock model IDs.
# export ORCHESTRATOR_MODEL=gemini-2.5-pro
# export SUB_AGENT_MODEL=gemini-2.5-flash
export FALLBACK_REGIONS=europe-west4,europe-west9,europe-west3,europe-central1

# Optional: use Amazon Bedrock Claude instead of Vertex AI Gemini
# export LLM_PROVIDER=bedrock
# export BEDROCK_MODEL_ID=eu.anthropic.claude-sonnet-4-5-20250929-v1:0
# export AWS_REGION_NAME=eu-central-1
# export AWS_BEARER_TOKEN_BEDROCK=...   # optional; standard AWS auth also works

export ENABLE_HISTORY_COMPACTION=true
export MAX_LLM_HISTORY_CONTENTS=32
export MAX_LLM_HISTORY_CHARS=120000

# Tina MCP, if testing tina_agent locally
export TINA_MCP_ENV=canary
export TINA_MCP_INTERNAL=false
export TINA_MCP_TIMEOUT_SECONDS=30
export TINA_MCP_SSE_READ_TIMEOUT_SECONDS=300
# export TINA_COGNITO_AUTH_FLOW=CLIENT_CREDENTIALS
# export TINA_COGNITO_CLIENT_ID=...
# export TINA_COGNITO_CLIENT_SECRET=...
# export TINA_COGNITO_TOKEN_ENDPOINT=...
# export TINA_COGNITO_SCOPES="https://mcp.localhost/mcp/tools.read https://mcp.localhost/mcp/tools.write"

# Development UI (NOT for production: no auth)
adk web

# Or the API server the Pipeline will hit:
adk api_server --host 0.0.0.0 --port 8000
```

`adk api_server` exposes:
- `POST /apps/{app}/users/{user}/sessions/{session}` — create session
- `GET  /apps/{app}/users/{user}/sessions/{session}` — fetch session
- `POST /run_sse` — execute a turn with SSE streaming
- `GET  /list-apps` — health/discovery

The Pipeline uses exactly these endpoints.

## Model backend and history compaction

All agents import centralized role models from `common/runtime_context.py`, so switching model providers is an environment-only change:

- `LLM_PROVIDER=vertex_ai` builds a LiteLLM Router-backed Gemini model. `GOOGLE_CLOUD_LOCATION` is the primary region; `FALLBACK_REGIONS` is tried in order when Vertex returns rate-limit/resource-exhaustion errors. The default Compose model is `gemini-2.5-flash`.
- `LLM_PROVIDER=bedrock` builds a LiteLLM Bedrock model. The default is the EU Claude Sonnet inference profile `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`. Authentication can use `AWS_BEARER_TOKEN_BEDROCK` or the normal AWS SDK chain (profile, IAM role, static keys, Roles Anywhere, etc.).
- `ORCHESTRATOR_MODEL` optionally overrides the model used by root orchestrators.
- `SUB_AGENT_MODEL` optionally overrides the model shared by all sub-agents.
- If either role variable is unset, it falls back to `GEMINI_MODEL` for Vertex AI or `BEDROCK_MODEL_ID` for Bedrock.

`inject_date` is wired as `before_model_callback` on the orchestrator and sub-agents. Before each model call it:

1. Injects an authoritative current-date block so relative requests like "today", "this month", or "last week" are interpreted correctly.
2. Optionally compacts long Open WebUI chat history in the model request. This does **not** delete or mutate the persistent ADK session; it only trims the prompt sent to the current LLM call.

History compaction is enabled by default with:

```
ENABLE_HISTORY_COMPACTION=true
MAX_LLM_HISTORY_CONTENTS=32
MAX_LLM_HISTORY_CHARS=120000
```

If a chat has a long tool-heavy history, older content is omitted from the prompt while the full session remains in Postgres/ADK storage. The compactor also avoids starting retained history with dangling tool-call/tool-result parts.

## Pattern: long-running tools with approval

Tools that affect production (deploy, restart, update) follow this flow:

1. The agent calls the tool with parameters from the user.
2. The tool detects a protected environment and returns
   `{"status": "pending_approval", "intended_action": {...}}` **without executing**.
3. The agent's instruction forces it to call `request_approval(action, params, reason)`.
4. The Pipeline intercepts the `request_approval` function call and renders it as a markdown block with `> ⚠️ Approval required`.
5. The user replies `approve` (or `cancel`).
6. On the next turn the agent re-invokes the original tool with `confirmed=True`.

The advantage over an n8n-style loop is that the pending-action state lives in the ADK session (`tool_context.state["pending_action"]`), so it survives reconnects and timeouts.

## Apps currently implemented

The ADK runtime exposes multiple app directories:

- `platform_agent` — SRE/DevOps orchestrator and its platform sub-agents.
- `tina_agent` — contract-analysis orchestrator backed by the Tina MCP/RAG domain.

Set the Open WebUI Pipeline valve `ADK_APP_NAME` to the app directory you want
the pipeline to call.

### `tina_agent`

`tina_agent` is a thin orchestrator with one sub-agent:

- `contract_analyzer` — read-only contract analyzer backed by Tina MCP over
  Streamable HTTP. It answers questions about contract UUIDs, user/customer
  details, email addresses, products such as batteries, clauses, status, and
  contract metadata. The MCP domain owns RAG retrieval; the agent only builds
  precise MCP tool parameters and formats returned facts.
- The currently exposed Tina MCP tools are:
  `ask_contract_documents`, `chat_with_documents`, `search_documents`,
  `resolve_contract_ids`, `list_contract_document_types`,
  `list_document_types`, and `clear_conversation`.

Endpoint resolution:

```
TINA_MCP_ENV=canary
TINA_MCP_INTERNAL=false
```

With no `TINA_MCP_URL`, canary/live resolve to:

- canary public: `https://mcp-off.test.bullfinch.com/tina/mcp`
- live public: `https://mcp.test.bullfinch.com/tina/mcp`
- canary internal: `http://tina-mcp-service.tina-mcp-test-canary.svc.cluster.local`
- live internal: `http://tina-mcp-service.tina-mcp-test-live.svc.cluster.local`

Authentication:

- Set `TINA_MCP_BEARER_TOKEN` to pass a token directly.
- Otherwise set Cognito env vars. `USER_PASSWORD_AUTH` and
  `ADMIN_USER_PASSWORD_AUTH` use boto3 against Cognito IDP. `CLIENT_CREDENTIALS`
  uses `TINA_COGNITO_TOKEN_ENDPOINT` with the app client ID and secret.
- `TINA_COGNITO_SCOPES` must be space-delimited for OAuth
  client-credentials. The helper also normalizes comma-separated lists to
  prevent Cognito from dropping scopes.

The Tina MCP authorization header is resolved through ADK's dynamic MCP header
provider. When Cognito auth is used, the helper caches the token in memory and
refreshes it before expiry; tune the early refresh window with
`TINA_COGNITO_TOKEN_REFRESH_MARGIN_SECONDS`. `TINA_MCP_BEARER_TOKEN` remains a
static direct-token override and cannot be renewed by the agent.

The Tina MCP session can be slow to initialize. `TINA_MCP_TIMEOUT_SECONDS`
defaults to `30` so ADK does not drop the MCP toolset during a slow
Streamable HTTP handshake.

### `gitlab_agent`

Native tools (no MCP), uses `python-gitlab`:

- **Discovery**: `list_subgroups`, `list_group_projects`, `search_projects`
- **Pipeline ops**: `list_recent_pipelines`, `get_pipeline_status`,
  `trigger_deployment_pipeline` (with approval gate for protected envs)
- **Read-only CI/CD diagnostics**: `get_project_ci_overview`,
  `summarize_failed_jobs`, `get_job_trace`, `list_open_merge_requests`,
  `get_merge_request`, `list_tags`, `list_environments`,
  `get_environment_deployments`

### `datadog_agent`

Wraps the official Datadog HTTP MCP server (`mcp.datadoghq.eu`). Capabilities depend on what the MCP exposes — typically: services, logs search, APM spans/traces, metrics queries, hosts and clusters listing, monitors. The agent is read-only by design; it correlates signals and produces investigation summaries with suggested next steps.

The Datadog MCP server is reached over Streamable HTTP with custom auth headers (`DD-API-KEY`, `DD-APPLICATION-KEY`). No subprocess is spawned — the MCP session is a long-lived HTTP connection.

### `aws_cost_agent`

Native tools (no MCP), uses `boto3` to call AWS Cost Explorer. Read-only by design. The agent is multi-account aware: it runs with credentials of the management account so Cost Explorer sees all linked accounts, and 12-digit account IDs are automatically translated to human aliases on output.

Tools:

- `get_cost_summary(start_date, end_date, granularity, group_by)`
- `compare_periods(period_a_start, period_a_end, period_b_start, period_b_end, group_by, top_n)`
- `get_top_cost_drivers(start_date, end_date, top_n, group_by)`
- `forecast_costs(start_date, end_date, granularity)`
- `export_cost_report_csv(start_date, end_date, granularity, group_by, filename)`

**Authentication** is via standard boto3 resolution. In production we use IAM Roles Anywhere with a profile in `~/.aws/config` whose `credential_process` calls the AWS signing helper. The container picks up `AWS_PROFILE` from env and finds everything via the mounted `~/.aws` and certs directories. No long-lived static keys.

**Required IAM policy** (least-privilege, read-only):

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

**Account alias mapping**: configure with the env var `AWS_ACCOUNT_ALIASES` formatted as `id1=alias1,id2=alias2,...`. Example for a Bullfinch-style org:

```
AWS_ACCOUNT_ALIASES=111111111111=bullfinch-root-master,222222222222=bullfinch-aws-docshare,333333333333=bullfinch-aws-sharedservices,444444444444=bullfinch-aws-warehouse,555555555555=bullfinch-dev-apollo,666666666666=bullfinch-prod-apollo,777777777777=bullfinch-security-logarchive,888888888888=bullfinch-security-audit,999999999999=bullfinch-aws-staging
```

If an account ID appears in results without a matching alias, the raw 12-digit ID is returned and the agent flags the gap to the user.

**CSV reports**: written to `REPORTS_DIR` (default `/app/reports`). Mount this as a volume so users can retrieve files from the host.

### `hetzner_cost_agent`

Native tools (no MCP), uses `httpx` against the Hetzner Cloud REST API. Read-only by design. Single-project scope (one API token = one project).

Tools:

- `get_hetzner_cost_summary(resource_types, label_filter)` — steady-state monthly cost broken down by resource type
- `list_hetzner_resources(resource_type, label_filter, sort_by_cost, limit)` — detailed inventory of one resource type
- `get_hetzner_top_cost_drivers(top_n, resource_types, label_filter)` — most expensive individual resources
- `get_hetzner_pricing(resource_type, name_filter, location)` — what-if catalogue lookup (for resources you don't own yet)

**Cost model**: "steady-state monthly" — for every currently active resource we sum the monthly net price the API returns. This answers "if nothing changes, what will we pay this month?". It is **not** the prorated spend for the elapsed portion of the month. Hetzner caps usage at the monthly price, so this figure is an upper bound on actual spend for resources created mid-month.

**Resources covered**:

| Type           | Paid? | Notes                                                                    |
|----------------|-------|--------------------------------------------------------------------------|
| `server`       | yes   | Price varies by type and location                                        |
| `load_balancer`| yes   | Price varies by type and location                                        |
| `volume`       | yes   | Block storage, priced per GB-month                                       |
| `primary_ip`   | yes   | IPs attached or reserved                                                 |
| `floating_ip`  | yes   | Currently not used by the team but supported for future                  |
| `network`      | free  | Tracked as inventory                                                     |
| `firewall`     | free  | Tracked as inventory                                                     |
| `certificate`  | free  | Tracked as inventory                                                     |

**Required env**:

```
HETZNER_TOKEN=...   # API token with read access on the project
```

The token is generated in the Hetzner Cloud Console (Project → Security → API Tokens). Read-only scope is sufficient for this agent.

**Currency and VAT**: all prices are in **EUR, NET** (VAT-excluded). The agent always states this explicitly in headline figures.

### `hetzner_action_agent`

Native tools using `httpx`. **Mutating** but narrowly scoped — two execution paths:

**PATH A — Direct Hetzner API**: soft reboot (ACPI) and hard power cycle (poweroff + poweron) of single servers identified by Hetzner labels (`cluster=`, `service=`). Used for reactive ad-hoc actions. No SSH involved.

**PATH B — Ansible jobs via GitLab pipeline**: triggers parameterized pipelines in the Ansible repo. The catalog of available jobs lives in `job.yml` at the root of the Ansible repo (`bullfinch-capital/ops/ansible`); the agent reads it via GitLab API and uses it for both validation and discovery. **Adding a new job to the Ansible repo exposes it without any code change in the agent** — that's the whole point.

**No SSH from the agent**. Direct OS access has been removed. All OS-level operations (apt update + upgrade, kubectl read on bootstrap nodes, etc.) go through Ansible jobs that run on GitLab runners *inside* the Hetzner private network.

Tools:

- `reboot_servers(reason, cluster, service, server_names, role, sequential, ...)` — PATH A: graceful Hetzner-API reboot
- `power_cycle_servers(...)` — PATH A: hard power cycle (use when soft fails)
- `check_recent_reboot_actions(...)` — PATH A: verify Hetzner action completion; session-state recovery
- `list_ansible_jobs()` — PATH B: read the catalog (host_groups + jobs) from the Ansible repo
- `run_ansible_job(job_name, reason, nodes, confirmed, confirmed_value)` — PATH B: trigger pipeline. Validates job_name against catalog, validates `nodes` against `allowed_target_groups.valid_values`
- `check_ansible_job_status(pipeline_id, include_logs)` — PATH B: pipeline + Ansible job status with log tail
- `request_approval(action, params, reason)` — sentinel for approval UI

**Server identification — two label families** (PATH A only):

- `cluster=<name>` (+ optional `role=<master|worker>`) for K8s nodes
- `service=<name>` for standalone servers (e.g. `nat-gateway-dev`, `gitlab-runner-generic-1`)

PATH B uses **Ansible host names** (e.g. `helios-1`, `rke2-prod-1`), validated against the catalog's `host_groups.valid_values`.

**Catalog-driven safety (PATH B)**:

1. **`name` ↔ `OPS_AI_AGENT` convention**: each job's `name` in `job.yml` is the value passed to the pipeline as `OPS_AI_AGENT`. The pipeline rules (`if: '$OPS_AI_AGENT == ...'`) select the right ansible-playbook invocation.

2. **`allowed_target_groups`**: each job declares which `host_groups` can supply `NODES_AI_AGENT`. The agent validates the user-supplied node list against the union of `valid_values` from those groups. Hosts outside the allowed groups are rejected before triggering the pipeline.

3. **`is_management: true`** on a host_group propagates typed-confirmation requirement to ANY job whose `allowed_target_groups` includes that group. Defense in depth: a job that doesn't declare typed_confirmation explicitly still gets it if it touches a management group.

4. **GitLab runners refused**: hosts named `gitlab-runner-*` cannot be passed as `nodes` to Ansible jobs (self-execution risk — runner cannot upgrade itself while running the upgrade job). The tool refuses at validation time. Runners are managed manually.

**Required env**:

```
HETZNER_TOKEN=...                               # for PATH A
KUBE_MANAGEMENT_CLUSTERS=rancher-dev,rancher-prod   # PATH A typed-confirmation source

GITLAB_TOKEN=...                                # for PATH B (PAT with api scope)
GITLAB_BASE_URL=https://gitlab.com              # optional; defaults to gitlab.com
ANSIBLE_PROJECT_PATH=bullfinch-capital/ops/ansible
ANSIBLE_REPO_REF=main                           # optional; defaults to main
ANSIBLE_JOB_CATALOG_PATH=job.yml                # optional; defaults to job.yml at root

ACTION_LOG_DIR=/app/action_logs                 # optional; audit log JSONL
```

**Catalog (job.yml) overview** — currently 14 jobs:

| Category | Jobs | Notes |
|---|---|---|
| Read-only | `get_nodes_info_dev`, `get_nodes_info_prod` | `kubectl get nodes -o wide` from bootstrap node |
| Reboot helios | `reboot_nodes_dev`, `reboot_nodes_prod` | Sequential per node |
| Reboot rancher | `reboot_nodes_rke_dev`, `reboot_nodes_rke_prod` | Typed-name confirmation |
| Hard power cycle | `power_off_on_helios_node_*`, `power_off_on_rke_node_*` | All nodes simultaneously — full outage |
| Upgrade (NEW) | `upgrade_nodes_*`, `upgrade_nodes_rke_*` | apt update + upgrade + conditional reboot |

**Long-running operation handling**:

- PATH A reboots: Hetzner API returns immediately with action_id. VM-level reboot 30-90s. Tool returns "executed" once API calls fired. `check_recent_reboot_actions` verifies completion.
- PATH B pipelines: anywhere from 1 minute (`get_nodes_info`, single kubectl call) to 30+ minutes (full upgrade of multi-node cluster). Pipeline runs on GitLab runners; agent doesn't block on it. `check_ansible_job_status` returns current state + log tail at any point. Chat disconnects don't affect the running pipeline.

**Excluded from catalog**:

- Bullfinch MCP cluster — not yet managed via Ansible (managed via Rancher dev as control plane). `k8s_analysis_agent` can still inspect it read-only.
- Helios DS / Rancher DS (disaster recovery sites) — being decommissioned; not in the catalog.

### `k8s_analysis_agent`

Native tools (no MCP), uses the official `kubernetes` Python client. Read-only by design — inspects but does not mutate. Talks to all configured clusters (workload + management) for analysis purposes; the future action agent will be allowed to operate only on workload clusters.

Tools (8):

- `cluster_overview(cluster)` — fleet/cluster headline state
- `analyze_node_health(cluster, node_name)` — capacity vs requests, conditions, optional metrics-server usage
- `analyze_namespace(cluster, namespace)` — workloads, pods, events, PVCs, configmap METADATA, services, ingresses
- `analyze_workload(cluster, namespace, kind, name)` — deep-dive for deployment/statefulset/daemonset/job/cronjob
- `find_failing_workloads(cluster, namespace)` — hunt for crashlooping, image-pull failures, under-replicated deploys, failed jobs, stale cronjobs
- `get_recent_events(cluster, namespace, since_minutes, warnings_only, limit)` — recent cluster events (warnings-only by default)
- `inspect_configmap(cluster, namespace, name)` — ConfigMap metadata + sensitivity-flagged key list. **Never returns values.**
- `list_persistent_volumes(cluster, namespace)` — PV/PVC inventory

**Strict security guarantees enforced in code, not just prompt**:

1. The Rancher `View Only` role used by the agent's user excludes Secrets at the API level — even if the model tried, the API would reject it.
2. `inspect_configmap` returns metadata only. There is no parameter, override, or escape hatch to read values. To inspect a value the user must `kubectl get cm` from the cluster directly.
3. All tools call read-only verbs (`get`/`list`/`watch`). The handoff to a future action agent is intentionally explicit; this agent's Rancher role cannot be widened to write.

**Cluster registry** is configured via two env vars:

```
KUBE_CLUSTERS=helios-dev=/etc/kube/helios-dev.yaml,bullfinch-mcp=/etc/kube/bullfinch-mcp.yaml,helios-prod=/etc/kube/helios-prod.yaml,rancher-dev=/etc/kube/rancher-dev.yaml,rancher-prod=/etc/kube/rancher-prod.yaml
KUBE_MANAGEMENT_CLUSTERS=rancher-dev,rancher-prod
```

Anything in `KUBE_CLUSTERS` not listed in `KUBE_MANAGEMENT_CLUSTERS` is treated as a workload cluster. The current agent doesn't mutate anything regardless, but the registry exposes `is_workload_cluster()` so the future action agent can refuse management clusters.

**Provisioning agent access**: this fleet uses Rancher as a proxy in front of every cluster (the cluster API endpoints are paths under `rancher-{env}.platform.bullfinch.com`). Rancher authenticates clients with its own credential system, not native K8s ServiceAccount tokens. The provisioning flow uses **Rancher API tokens scoped per cluster** instead of K8s SAs.

See `k8s/README-rancher-setup.md` for the full operator procedure: create the agent user, grant per-cluster `View Only` role, generate scoped API tokens, build one kubeconfig per cluster with the corresponding token, mount into the container.

The agent code (`cluster_registry.py`) is auth-agnostic — it loads kubeconfigs and calls the K8s API. Whether the kubeconfig contains a Rancher token or a native SA token doesn't matter to the client.

**Datadog handoff pattern**: this agent never calls Datadog directly. When log/APM/metrics correlation would help, it ends its turn with an explicit recommendation ("for log analysis ask the Datadog agent with filters service:X env:prod"). The orchestrator picks up the handoff and delegates to `datadog_agent`. This keeps each agent's tool surface small and avoids cross-agent direct dependencies.

## Adding a new sub-agent (template)

For an MCP-backed agent (HTTP transport, like Datadog):

```python
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset

toolset = MCPToolset(
    connection_params=StreamableHTTPConnectionParams(
        url="https://mcp.example.com/mcp",
        headers={"X-API-KEY": os.environ["EXAMPLE_KEY"]},
    ),
)

example_agent = LlmAgent(
    name="example_agent",
    model=LiteLlm(model="vertex_ai/gemini-2.5-pro"),
    description="...",
    instruction="...",
    tools=[toolset],
)
```

For an MCP-backed agent (stdio transport, e.g. an MCP server you launch as a subprocess):

```python
from google.adk.tools.mcp_tool.mcp_session_manager import StdioServerParameters

toolset = MCPToolset(
    connection_params=StdioServerParameters(
        command="npx",
        args=["-y", "@some/mcp-server"],
        env={"SOME_KEY": "..."},
    ),
)
```

Then add the new agent to `sub_agents=[...]` in that app's orchestrator. No
Pipeline code change is needed when adding a sub-agent to an existing app. A
new top-level app should get its own Pipeline file or valve configuration with
`ADK_APP_NAME` set to the new app directory.

## Container

Minimal Dockerfile for the ADK runtime:

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY platform_agent ./platform_agent
COPY tina_agent ./tina_agent
COPY common ./common

EXPOSE 8000
CMD adk api_server \
      --host 0.0.0.0 \
      --port 8000 \
      --session_service_uri "${ADK_SESSION_DB_URL}" \
      .
```

Important direct runtime dependencies in `requirements.txt`:

- `google-adk` — ADK runtime and API server.
- `litellm` — Vertex AI / Bedrock model adapter.
- `mcp` — required by ADK MCP toolsets.
- `asyncpg` and `sqlalchemy` — required for Postgres-backed ADK sessions.
- `python-gitlab`, `boto3`, `httpx`, `kubernetes`, `pyyaml` — native tool clients.

The Kubernetes Python client is pinned intentionally. Rancher token
kubeconfigs can expose authentication differences between `kubectl` and the
generated Python client when the client major version changes; upgrade it only
after rebuilding the image and smoke-testing the Kubernetes analysis tools.

`requirements-lock.txt` captures the full package set from the validated
platform-agent image. Use it for reproducible Docker/local builds; refresh it
only as part of a deliberate dependency upgrade.

## Security notes

- **Secrets** (`GITLAB_TOKEN`, `DD_*`, AWS credentials): use Kubernetes Secrets / Docker secrets, never bake into the image.
- **GCP service account**: prefer Workload Identity on GKE over a static JSON file.
- **Tina Cognito credentials**: keep client secrets/passwords in secrets storage. Do not commit `.env` with real Cognito values.
- **K8s ops sub-agent** (when added): use a K8s ServiceAccount with tight RBAC, **not** cluster-admin.
- **Approval gate**: every tool with destructive effects on production must use the `pending_approval` + `request_approval` pattern. Never execute destructive actions on first invocation.
- **Datadog Application Key scopes**: scope the application key to read-only roles (`logs_read_data`, `apm_read`, `metrics_read`, `monitors_read`). The agent never needs write scopes.
