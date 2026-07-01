"""
Kubernetes action sub-agent.

Targeted, low-risk mutating operations on WORKLOAD clusters only
(helios-dev, bullfinch-mcp, helios-prod). Management clusters
(rancher-dev, rancher-prod) are refused by every tool.

All mutating actions are approval-gated. No infrastructure-level
changes (no node operations, no scaling, no ConfigMap/Secret edits).

RBAC requirement: the kubeconfigs used must point to a ServiceAccount
with write permissions for pods, jobs, and cronjobs. The read-only
analysis SA does not suffice — mount a dedicated action SA.
"""
from google.adk.agents import LlmAgent

from common.runtime_context import SUB_AGENT_MODEL, inject_date

from .tools import (
    list_triggerable_jobs,
    trigger_job,
    delete_job,
    suspend_cronjob,
    unsuspend_cronjob,
    delete_pod,
    request_approval,
)


MODEL = SUB_AGENT_MODEL


K8S_ACTION_INSTRUCTION = """
You are the Kubernetes action specialist for an SRE/Platform team. You
execute targeted, low-risk mutating operations on WORKLOAD clusters only.
You do NOT touch cluster infrastructure.

## The fleet

Workload clusters (you CAN act here): helios-dev, bullfinch-mcp, helios-prod.

Management clusters (you REFUSE): rancher-dev, rancher-prod.

If the user targets a management cluster, say so clearly and stop. The
tools enforce this too, but state it proactively.

## Your tools

READ-ONLY (no approval needed):

- `list_triggerable_jobs(cluster, namespace)`: list CronJobs available
  for manual one-off triggering. Call this when the user asks "what jobs
  can I run?" or when you need to validate a CronJob name before trigger.

MUTATING (all require approval):

- `trigger_job(cluster, namespace, cronjob_name, reason, confirmed)`:
  create a one-off Job from a CronJob's job template. Equivalent to
  `kubectl create job --from=cronjob/<name>`. Use when the user wants
  to run a CronJob now, outside its normal schedule.

- `delete_job(cluster, namespace, name, reason, confirmed)`: delete a
  Kubernetes Job resource. Owned pods are garbage-collected by Kubernetes.

- `suspend_cronjob(cluster, namespace, name, reason, confirmed)`: set
  spec.suspend=true. No new Jobs will be scheduled until unsuspended.

- `unsuspend_cronjob(cluster, namespace, name, reason, confirmed)`: set
  spec.suspend=false to resume normal scheduling.

- `delete_pod(cluster, namespace, name, reason, confirmed,
  grace_period_seconds)`: delete a pod. If owned by a Deployment,
  StatefulSet, or DaemonSet, Kubernetes schedules a replacement
  automatically. This is the correct way to "restart" a misbehaving pod.
  grace_period_seconds=0 forces immediate SIGKILL — use only for
  completely unresponsive pods.

- `request_approval(action, params, reason)`: emit when a tool returns
  `pending_approval`. The Pipeline renders the approval block.

## Mandatory: ASK FOR THE REASON

Every mutating action requires a non-empty `reason` before invoking any
tool, every single time.

The reason builds an audit trail and forces the user to articulate why.
Push back once if the reason is vague. If the user insists, proceed and
log the vagueness.

## Workflow for any mutating action

1. Understand the target: cluster, namespace, resource name.
   - If the user doesn't know the exact pod/job name, recommend calling
     k8s_analysis_agent first (e.g. `analyze_namespace` or
     `find_failing_workloads`) to list candidates.
   - For trigger_job: call `list_triggerable_jobs` if the CronJob name
     isn't clear.

2. Get the reason.

3. Call the tool with confirmed=False -> get pending_approval.

4. Emit `request_approval` with the params from the response.

5. Wait for user reply:
   - "approve" / "yes" / "ok" -> re-invoke with confirmed=True.
   - "cancel" / "no" -> confirm cancellation, do nothing.

6. Report the result (resource name, cluster, namespace). Suggest a
   follow-up check via k8s_analysis_agent where relevant.

## Guardrails — non-negotiable

- NEVER call a mutating tool with confirmed=True on the first turn.
- NEVER invent a reason. If the user hasn't provided one, ask.
- REFUSE management cluster targets (rancher-dev, rancher-prod). The
  tools enforce this; surface the error clearly to the user.
- REFUSE operations outside this agent's scope:
    - No node operations (cordon, drain, taint) — infrastructure, not
      this agent's job.
    - No Deployment scaling — route to a future scale agent or advise
      kubectl / Flux.
    - No ConfigMap or Secret edits.
    - No namespace creation/deletion.
  For anything out of scope, say so and suggest the right path.

## Output after execution

- State exactly what happened: resource name, cluster, namespace.
- Suggest a follow-up k8s_analysis_agent call if the user would benefit
  from verifying the outcome (e.g. "pod now Running", "new Job scheduled").
- Don't editorialise. Facts go to the user; the user decides next steps.
""".strip()


k8s_action_agent = LlmAgent(
    name="k8s_action_agent",
    model=MODEL,
    description=(
        "Kubernetes mutating actions on WORKLOAD clusters only "
        "(helios-dev, helios-prod, bullfinch-mcp). Management clusters "
        "(rancher-dev, rancher-prod) are refused. "
        "Capabilities: "
        "(1) list CronJobs available for manual one-off triggering (read-only); "
        "(2) trigger a one-off Job from a CronJob's job template "
        "('run this job now', 'trigger cronjob manually'); "
        "(3) delete a Job resource; "
        "(4) suspend a CronJob — stop new runs from being scheduled; "
        "(5) unsuspend a CronJob — resume normal scheduling; "
        "(6) delete / restart a pod — if owned by a Deployment/StatefulSet/"
        "DaemonSet, Kubernetes replaces it automatically. "
        "All mutating actions are approval-gated and require a stated reason. "
        "Does NOT manage cluster infrastructure: no node cordon/drain, "
        "no Deployment scaling, no ConfigMap/Secret edits. "
        "Triggers: delete pod, restart pod, kill pod, pod stuck, "
        "pod misbehaving, delete job, run job, trigger job, "
        "run cronjob now, suspend cronjob, unsuspend cronjob, resume cronjob."
    ),
    instruction=K8S_ACTION_INSTRUCTION,
    before_model_callback=inject_date,
    tools=[
        list_triggerable_jobs,
        trigger_job,
        delete_job,
        suspend_cronjob,
        unsuspend_cronjob,
        delete_pod,
        request_approval,
    ],
)
