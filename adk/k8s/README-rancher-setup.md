# Kubernetes setup — Rancher API token flow

This is the operator-facing procedure to provision the platform-agent's
read-only access to the cluster fleet. The agent does NOT use Kubernetes
ServiceAccount tokens directly: every cluster in this environment is
fronted by a Rancher proxy, so we use **Rancher API tokens** scoped per
cluster.

The result is one kubeconfig file per cluster, mounted into the
platform-agent container.

## Why Rancher tokens (and not K8s SA tokens)

The cluster API endpoints exposed publicly are Rancher proxies (e.g.
`https://rancher-dev.platform.bullfinch.com/k8s/clusters/c-m-XXXXXXXX`).
The Rancher proxy authenticates clients with **Rancher's own credential
system**, not with the downstream cluster's native ServiceAccount
JWTs. A SA token created on the downstream cluster is rejected by the
proxy as `system:unauthenticated`.

Trade-offs of this approach:

- ✅ Uses the auth mechanism Rancher is designed for; works through the proxy with no infra changes.
- ✅ Tokens are scoped per-cluster in the Rancher UI: a leaked token affects exactly one cluster.
- ✅ Audit log and rotation are first-class in Rancher.
- ⚠️ Tokens must be regenerated manually from the UI (no native API for non-admin self-service rotation in most Rancher versions).
- ⚠️ The agent's identity becomes a Rancher user, not a K8s SA. This means the audit trail at the cluster level shows the Rancher proxy SA, and you correlate to the human-named Rancher user via Rancher's audit log.

If you ever expose downstream K8s API servers directly (bypassing
Rancher) you can switch back to native ServiceAccounts; the agent code
will work either way.

## Prerequisites

- A Rancher admin account on both Rancher instances (`rancher-dev` and `rancher-prod`).
- All target clusters visible in the Rancher UI: `helios-dev`, `bullfinch-mcp` (in rancher-dev), `helios-prod` (in rancher-prod), and the two Rancher instances themselves as `local` clusters.

## One-time provisioning

Do this once, plus whenever a token expires.

### 1. Create the agent user in Rancher

In **rancher-dev** (admin login):

- Top-right user menu → **Users & Authentication** → **Users** → **Create**
- Username: `platform-agent-analyzer`
- Password: any strong random string (you won't reuse it)
- Global Role: **User-Base** (NOT Admin)
- Save

Repeat the same in **rancher-prod**. The user is local to each Rancher
instance — there's no shared identity unless you've configured an
external IdP, in which case you can use the same identity instead.

### 2. Grant cluster access (read-only)

In **rancher-dev**:

For each cluster (`local`, `helios-dev`, `bullfinch-mcp`):

- Cluster Management → click the cluster name → **Members** tab → **Add**
- User or Group: `platform-agent-analyzer`
- Role: **View Only** (this Rancher built-in role grants list/get/watch on
  most resources, **excludes Secrets**, and forbids any mutating verb)
- Add

In **rancher-prod**, do the same for `local` and `helios-prod`.

> **Note on `View Only` and ConfigMaps**: this role does include `get`/`list`
> on ConfigMaps. The agent's tool layer (`inspect_configmap`) defends in
> depth by NEVER returning ConfigMap values, only metadata. If you want
> to harden further, you can create a custom Cluster Role in Rancher that
> excludes ConfigMaps too, but you'll lose the ability to inventory them.

### 3. Generate API tokens (one per cluster)

Logout from the admin account. Login as `platform-agent-analyzer` in
**rancher-dev**.

For each cluster the user has access to (`local`, `helios-dev`, `bullfinch-mcp`):

- Top-right user menu → **Account & API Keys** → **Create API Key**
- Description: e.g. `platform-agent — helios-dev`
- **Scope**: select the specific cluster (NOT "no scope")
- Expires: choose a duration (90 days recommended; "Never" not advised)
- Create

Rancher shows the token **once**. Copy the value — it looks like:

```
token-abcde:1234567890abcdef...
```

Save it temporarily in a secrets manager / password manager. Repeat for
each cluster, in each Rancher instance. You should end up with 5 tokens
(one per cluster: `local-dev`, `helios-dev`, `bullfinch-mcp`,
`local-prod`, `helios-prod`).

### 4. Build the per-cluster kubeconfigs

For each cluster, in the Rancher UI:

- Click the cluster name
- Top-right "Kubeconfig File" button (or three-dots menu → "Download Kubeconfig")
- Save the file

The downloaded kubeconfig is admin-scoped (uses your admin token). You
need to **replace the token** with the agent's token from step 3.

Open the file and find the `users:` block:

```yaml
users:
  - name: helios-dev
    user:
      token: <ADMIN-TOKEN-HERE>
```

Replace `<ADMIN-TOKEN-HERE>` with the token generated in step 3 for that
specific cluster. Save as e.g. `kubeconfigs/helios-dev.yaml`.

Repeat for all 5 clusters.

### 5. Verify

```bash
for KC in kubeconfigs/*.yaml; do
  echo "=== $KC ==="
  KUBECONFIG=$KC kubectl get nodes -o wide 2>&1 | head -5
done
```

Each kubeconfig should successfully list nodes. If you see:

- `Unable to connect to the server: tls: failed to verify certificate`:
  the public CA that signs the Rancher endpoint isn't trusted by your
  local OS. Either install the CA system-wide, or extract
  `certificate-authority-data` from a working admin kubeconfig and add
  it under the `clusters[0].cluster:` block.
- `Unauthorized` / 401: token wrong or expired. Re-generate from step 3.
- `Forbidden` / 403 on specific resources: the View Only role doesn't
  cover them. Most analysis tools work without those, but if it's a
  resource you care about (e.g. `metrics.k8s.io`) talk to the Rancher
  admin about extending the role.

### 6. Mount into the platform-agent

Place the 5 kubeconfigs in `./kubeconfigs/` (relative to your
docker-compose project) and add to `docker-compose.yml`:

```yaml
services:
  platform-agent:
    # ... existing config ...
    volumes:
      - ./kubeconfigs:/etc/kube:ro
    environment:
      - KUBE_CLUSTERS=helios-dev=/etc/kube/helios-dev.yaml,bullfinch-mcp=/etc/kube/bullfinch-mcp.yaml,helios-prod=/etc/kube/helios-prod.yaml,rancher-dev=/etc/kube/rancher-dev.yaml,rancher-prod=/etc/kube/rancher-prod.yaml
      - KUBE_MANAGEMENT_CLUSTERS=rancher-dev,rancher-prod
```

In Kubernetes (production), mount as a Secret instead of a host volume:

```bash
kubectl create secret generic platform-agent-kubeconfigs \
  --from-file=helios-dev.yaml=kubeconfigs/helios-dev.yaml \
  --from-file=bullfinch-mcp.yaml=kubeconfigs/bullfinch-mcp.yaml \
  --from-file=helios-prod.yaml=kubeconfigs/helios-prod.yaml \
  --from-file=rancher-dev.yaml=kubeconfigs/rancher-dev.yaml \
  --from-file=rancher-prod.yaml=kubeconfigs/rancher-prod.yaml \
  -n platform-agent
```

Then mount the Secret at `/etc/kube` in the platform-agent Deployment.

## Token rotation

When tokens approach expiry:

1. Login as `platform-agent-analyzer` in the relevant Rancher instance
2. Generate new tokens (step 3) for the affected cluster(s)
3. Update the corresponding kubeconfig file(s) with the new token value
4. If using docker-compose, restart the platform-agent service: `docker compose restart platform-agent`
5. If using K8s Secrets, recreate the Secret and let the pod restart pick it up

The agent's `_clients_cache` is rebuilt on container restart, so the new
tokens take effect immediately.

## .gitignore

The kubeconfigs contain valid bearer tokens. Never commit them.

```
# .gitignore
kubeconfigs/
*.kubeconfig
```
