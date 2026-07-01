"""
Native ADK GitLab tools.

These replace the custom GitLab MCP server: same capabilities, without an
extra network hop and with clean stack traces when something fails.

Granularity: tools are "business operations" (for example trigger_deployment),
not low-level API endpoints. This reduces the number of LLM iterations and
therefore reasoning loops.

Approval pattern for destructive actions:
  - Actions that modify production are NOT executed directly.
  - The tool returns a 'pending_approval' status and the agent emits a
    request_approval(...) function call, which the Pipeline translates into a
    UI approval block for the user.
  - When the user replies 'approve', the agent calls the tool again with
    confirmed=True and the action is executed.
"""
from __future__ import annotations

import os
import re
from typing import Optional

import gitlab  # python-gitlab
from google.adk.tools import ToolContext


# ----------------------------------------------------------------------
# Client GitLab (lazy, singleton)
# ----------------------------------------------------------------------

_gl_client: Optional[gitlab.Gitlab] = None
_FAILED_JOB_STATUSES = {"failed"}
_MAX_TRACE_LINES = 1000
_MAX_TRACE_CHARS = 30000
_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)((?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s]+"),
]


def _client() -> gitlab.Gitlab:
    global _gl_client
    if _gl_client is None:
        url = os.environ.get("GITLAB_URL", "https://gitlab.com")
        token = os.environ.get("GITLAB_TOKEN")
        if not token:
            raise RuntimeError(
                "GITLAB_TOKEN is not set. Configure it as a deployment secret."
            )
        _gl_client = gitlab.Gitlab(url=url, private_token=token, timeout=30)
        _gl_client.auth()
    return _gl_client


def _limit(value: int, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _short_sha(sha: Optional[str]) -> Optional[str]:
    return sha[:8] if sha else None


def _field(obj, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _project_summary(project) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "path_with_namespace": project.path_with_namespace,
        "default_branch": getattr(project, "default_branch", None),
        "web_url": project.web_url,
        "last_activity_at": getattr(project, "last_activity_at", None),
        "archived": getattr(project, "archived", False),
    }


def _pipeline_summary(pipeline) -> dict:
    return {
        "id": pipeline.id,
        "ref": getattr(pipeline, "ref", None),
        "sha": _short_sha(getattr(pipeline, "sha", None)),
        "status": getattr(pipeline, "status", None),
        "source": getattr(pipeline, "source", None),
        "created_at": getattr(pipeline, "created_at", None),
        "updated_at": getattr(pipeline, "updated_at", None),
        "web_url": getattr(pipeline, "web_url", None),
    }


def _job_summary(job) -> dict:
    return {
        "id": job.id,
        "name": job.name,
        "stage": job.stage,
        "status": job.status,
        "duration": getattr(job, "duration", None),
        "web_url": getattr(job, "web_url", None),
    }


def _merge_request_summary(mr) -> dict:
    author = getattr(mr, "author", None) or {}
    return {
        "iid": mr.iid,
        "id": mr.id,
        "title": mr.title,
        "state": mr.state,
        "source_branch": mr.source_branch,
        "target_branch": mr.target_branch,
        "author": _field(author, "username"),
        "draft": getattr(mr, "draft", False) or getattr(mr, "work_in_progress", False),
        "merge_status": getattr(mr, "merge_status", None),
        "detailed_merge_status": getattr(mr, "detailed_merge_status", None),
        "created_at": getattr(mr, "created_at", None),
        "updated_at": getattr(mr, "updated_at", None),
        "web_url": mr.web_url,
    }


def _tag_summary(tag) -> dict:
    commit = getattr(tag, "commit", None) or {}
    return {
        "name": tag.name,
        "target": _short_sha(getattr(tag, "target", None)),
        "commit_short_id": _field(commit, "short_id") or _short_sha(_field(commit, "id")),
        "commit_title": _field(commit, "title"),
        "created_at": getattr(tag, "created_at", None),
        "web_url": getattr(tag, "web_url", None),
    }


def _environment_summary(environment, latest_deployment: Optional[dict] = None) -> dict:
    return {
        "id": environment.id,
        "name": environment.name,
        "state": getattr(environment, "state", None),
        "external_url": getattr(environment, "external_url", None),
        "web_url": getattr(environment, "web_url", None),
        "latest_deployment": latest_deployment,
    }


def _deployment_summary(deployment) -> dict:
    deployable = getattr(deployment, "deployable", None) or {}
    commit = getattr(deployment, "commit", None) or {}
    return {
        "id": deployment.id,
        "iid": getattr(deployment, "iid", None),
        "status": getattr(deployment, "status", None),
        "ref": getattr(deployment, "ref", None),
        "sha": _short_sha(getattr(deployment, "sha", None)),
        "created_at": getattr(deployment, "created_at", None),
        "updated_at": getattr(deployment, "updated_at", None),
        "deployable_status": _field(deployable, "status"),
        "deployable_url": _field(deployable, "web_url"),
        "commit_short_id": _field(commit, "short_id") or _short_sha(_field(commit, "id")),
        "web_url": getattr(deployment, "web_url", None),
    }


def _redact_trace(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


# ----------------------------------------------------------------------
# Tool: group and project discovery (read-only)
# ----------------------------------------------------------------------

def list_group_projects(
    group_path: str,
    include_subgroups: bool = True,
    search: Optional[str] = None,
    archived: bool = False,
    limit: int = 50,
) -> dict:
    """
    Lists projects in a GitLab group.

    For large groups, use `search` to filter; otherwise the result can be very
    large and consume many tokens.

    Args:
        group_path: Full group path (for example 'bullfinch-capital' or
                    'bullfinch-capital/services'). Do not include a trailing slash.
        include_subgroups: If True (default), recursively include projects from
                           subgroups. If False, include only direct group projects.
        search: Optional text filter on project name/path.
        archived: If True, include archived projects as well. Default False.
        limit: Maximum number of projects to return (default 50, max 100).

    Returns:
        dict with 'projects' (list) or 'error'. Each project includes:
        id, name, path_with_namespace, default_branch, web_url, last_activity_at.
    """
    try:
        limit = max(1, min(int(limit), 100))
        group = _client().groups.get(group_path)
        kwargs = {
            "per_page": limit,
            "include_subgroups": include_subgroups,
            "archived": archived,
            "order_by": "last_activity_at",
            "sort": "desc",
        }
        if search:
            kwargs["search"] = search
        projects = group.projects.list(**kwargs)
        return {
            "group": group.full_path,
            "include_subgroups": include_subgroups,
            "count": len(projects),
            "projects": [
                {
                    "id": p.id,
                    "name": p.name,
                    "path_with_namespace": p.path_with_namespace,
                    "default_branch": getattr(p, "default_branch", None),
                    "web_url": p.web_url,
                    "last_activity_at": getattr(p, "last_activity_at", None),
                    "archived": getattr(p, "archived", False),
                }
                for p in projects
            ],
        }
    except gitlab.exceptions.GitlabError as e:
        return {"error": f"GitLab API error: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


def list_subgroups(group_path: str, limit: int = 50) -> dict:
    """
    Lists DIRECT subgroups of a GitLab group (one level only).

    Useful for navigating the hierarchy incrementally instead of recursively
    downloading all projects. Typically the agent calls this first to understand
    the structure, then calls `list_group_projects` on the relevant subgroup.

    Args:
        group_path: Parent group path (for example 'bullfinch-capital').
        limit: Maximum number of subgroups to return (default 50, max 100).

    Returns:
        dict with 'subgroups' or 'error'. Each subgroup includes id, name,
        full_path, and web_url.
    """
    try:
        limit = max(1, min(int(limit), 100))
        group = _client().groups.get(group_path)
        subgroups = group.subgroups.list(per_page=limit, order_by="name", sort="asc")
        return {
            "parent_group": group.full_path,
            "count": len(subgroups),
            "subgroups": [
                {
                    "id": sg.id,
                    "name": sg.name,
                    "full_path": sg.full_path,
                    "web_url": sg.web_url,
                }
                for sg in subgroups
            ],
        }
    except gitlab.exceptions.GitlabError as e:
        return {"error": f"GitLab API error: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


def search_projects(
    query: str,
    group_path: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """
    Searches GitLab projects by name or path.

    Args:
        query: Search string (for example 'auth-service', 'api'). Partial
               case-insensitive match on name and path.
        group_path: If provided, limits the search to projects in that group
                    and its subgroups. If None, searches the whole GitLab
                    instance visible to the token.
        limit: Maximum number of results (default 20, max 100).

    Returns:
        dict with 'projects' or 'error'.
    """
    try:
        limit = max(1, min(int(limit), 100))
        gl = _client()
        if group_path:
            group = gl.groups.get(group_path)
            projects = group.projects.list(
                search=query,
                include_subgroups=True,
                per_page=limit,
                order_by="last_activity_at",
                sort="desc",
            )
        else:
            projects = gl.projects.list(
                search=query,
                per_page=limit,
                order_by="last_activity_at",
                sort="desc",
                membership=True,  # only projects the token can access
            )
        return {
            "query": query,
            "scope": group_path or "all_accessible",
            "count": len(projects),
            "projects": [
                {
                    "id": p.id,
                    "name": p.name,
                    "path_with_namespace": p.path_with_namespace,
                    "default_branch": getattr(p, "default_branch", None),
                    "web_url": p.web_url,
                    "last_activity_at": getattr(p, "last_activity_at", None),
                }
                for p in projects
            ],
        }
    except gitlab.exceptions.GitlabError as e:
        return {"error": f"GitLab API error: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


# ----------------------------------------------------------------------
# Tool: list pipelines (read-only, no approval required)
# ----------------------------------------------------------------------

def list_recent_pipelines(
    project_path: str,
    ref: Optional[str] = None,
    limit: int = 10,
) -> dict:
    """
    Lists recent pipelines for a GitLab project.

    Args:
        project_path: Full project path (for example 'group/subgroup/repo').
        ref: Optional branch or tag filter. If None, includes all refs.
        limit: Maximum number of pipelines to return (default 10, max 50).

    Returns:
        dict with a 'pipelines' list, or 'error' on failure.
    """
    try:
        limit = max(1, min(int(limit), 50))
        project = _client().projects.get(project_path)
        kwargs = {"per_page": limit, "order_by": "updated_at", "sort": "desc"}
        if ref:
            kwargs["ref"] = ref
        pipelines = project.pipelines.list(**kwargs)
        return {
            "pipelines": [
                {
                    "id": p.id,
                    "ref": p.ref,
                    "sha": p.sha[:8],
                    "status": p.status,
                    "source": p.source,
                    "created_at": p.created_at,
                    "updated_at": p.updated_at,
                    "web_url": p.web_url,
                }
                for p in pipelines
            ]
        }
    except gitlab.exceptions.GitlabError as e:
        return {"error": f"GitLab API error: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


# ----------------------------------------------------------------------
# Tool: get pipeline status (read-only)
# ----------------------------------------------------------------------

def get_pipeline_status(project_path: str, pipeline_id: int) -> dict:
    """
    Gets detailed pipeline status, including its jobs.

    Args:
        project_path: Full project path.
        pipeline_id: Numeric pipeline ID.
    """
    try:
        project = _client().projects.get(project_path)
        pipeline = project.pipelines.get(pipeline_id)
        jobs = pipeline.jobs.list(all=True)
        return {
            "id": pipeline.id,
            "status": pipeline.status,
            "ref": pipeline.ref,
            "sha": pipeline.sha,
            "duration_seconds": pipeline.duration,
            "web_url": pipeline.web_url,
            "jobs": [
                {
                    "id": j.id,
                    "name": j.name,
                    "stage": j.stage,
                    "status": j.status,
                    "duration": j.duration,
                    "web_url": j.web_url,
                }
                for j in jobs
            ],
        }
    except gitlab.exceptions.GitlabError as e:
        return {"error": f"GitLab API error: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


# ----------------------------------------------------------------------
# Tool: failed job diagnosis (read-only)
# ----------------------------------------------------------------------

def get_job_trace(
    project_path: str,
    job_id: int,
    max_lines: int = 300,
) -> dict:
    """
    Gets the tail of a GitLab job trace/log.

    Use this tool when the user asks why a job or pipeline failed. The trace is
    limited and redacted to reduce token usage and the risk of exposing secrets.

    Args:
        project_path: Full project path.
        job_id: Numeric job ID.
        max_lines: Maximum number of trailing lines to return (default 300,
                   max 1000).

    Returns:
        dict with trace_tail, returned line counts, and job metadata.
    """
    try:
        max_lines = _limit(max_lines, 300, _MAX_TRACE_LINES)
        project = _client().projects.get(project_path)
        job = project.jobs.get(job_id)
        raw_trace = job.trace()
        if isinstance(raw_trace, bytes):
            trace = raw_trace.decode("utf-8", errors="replace")
        else:
            trace = str(raw_trace or "")

        lines = trace.splitlines()
        tail_lines = lines[-max_lines:]
        trace_tail = _redact_trace("\n".join(tail_lines))
        truncated_chars = 0
        if len(trace_tail) > _MAX_TRACE_CHARS:
            truncated_chars = len(trace_tail) - _MAX_TRACE_CHARS
            trace_tail = trace_tail[-_MAX_TRACE_CHARS:]

        return {
            "project_path": project.path_with_namespace,
            "job": _job_summary(job),
            "total_lines": len(lines),
            "returned_lines": len(tail_lines),
            "truncated_lines": max(len(lines) - len(tail_lines), 0),
            "truncated_chars": truncated_chars,
            "trace_tail": trace_tail,
        }
    except gitlab.exceptions.GitlabError as e:
        return {"error": f"GitLab API error: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


def summarize_failed_jobs(
    project_path: str,
    pipeline_id: int,
    max_trace_lines_per_job: int = 120,
    max_failed_jobs: int = 5,
) -> dict:
    """
    Returns failed jobs for a pipeline with compact log tails.

    Args:
        project_path: Full project path.
        pipeline_id: Numeric pipeline ID.
        max_trace_lines_per_job: Trailing lines to include for each failed job
                                 (default 120, max 1000).
        max_failed_jobs: Maximum number of failed jobs to inspect
                         (default 5, max 20).
    """
    try:
        max_trace_lines_per_job = _limit(
            max_trace_lines_per_job,
            120,
            _MAX_TRACE_LINES,
        )
        max_failed_jobs = _limit(max_failed_jobs, 5, 20)
        project = _client().projects.get(project_path)
        pipeline = project.pipelines.get(pipeline_id)
        jobs = pipeline.jobs.list(all=True)
        failed_jobs = [j for j in jobs if j.status in _FAILED_JOB_STATUSES]

        summaries = []
        for job_ref in failed_jobs[:max_failed_jobs]:
            job = project.jobs.get(job_ref.id)
            raw_trace = job.trace()
            if isinstance(raw_trace, bytes):
                trace = raw_trace.decode("utf-8", errors="replace")
            else:
                trace = str(raw_trace or "")
            lines = trace.splitlines()
            trace_tail = _redact_trace("\n".join(lines[-max_trace_lines_per_job:]))
            if len(trace_tail) > _MAX_TRACE_CHARS:
                trace_tail = trace_tail[-_MAX_TRACE_CHARS:]

            summaries.append(
                {
                    "job": _job_summary(job),
                    "total_lines": len(lines),
                    "returned_lines": min(len(lines), max_trace_lines_per_job),
                    "truncated_lines": max(len(lines) - max_trace_lines_per_job, 0),
                    "trace_tail": trace_tail,
                }
            )

        return {
            "project_path": project.path_with_namespace,
            "pipeline": _pipeline_summary(pipeline),
            "failed_job_count": len(failed_jobs),
            "returned_failed_job_count": len(summaries),
            "failed_jobs": summaries,
        }
    except gitlab.exceptions.GitlabError as e:
        return {"error": f"GitLab API error: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


# ----------------------------------------------------------------------
# Tool: merge requests (read-only)
# ----------------------------------------------------------------------

def list_open_merge_requests(
    project_path: str,
    target_branch: Optional[str] = None,
    author_username: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """
    Lists open merge requests for a project.

    Args:
        project_path: Full project path.
        target_branch: Optional target branch.
        author_username: Optional author username.
        limit: Maximum number of results (default 20, max 100).
    """
    try:
        limit = _limit(limit, 20, 100)
        project = _client().projects.get(project_path)
        kwargs = {
            "state": "opened",
            "per_page": limit,
            "order_by": "updated_at",
            "sort": "desc",
        }
        if target_branch:
            kwargs["target_branch"] = target_branch
        if author_username:
            kwargs["author_username"] = author_username
        merge_requests = project.mergerequests.list(**kwargs)
        return {
            "project_path": project.path_with_namespace,
            "count": len(merge_requests),
            "merge_requests": [_merge_request_summary(mr) for mr in merge_requests],
        }
    except gitlab.exceptions.GitlabError as e:
        return {"error": f"GitLab API error: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


def get_merge_request(
    project_path: str,
    mr_iid: int,
    include_changes: bool = False,
    max_changed_files: int = 50,
) -> dict:
    """
    Gets merge request details and, optionally, changed files.

    Args:
        project_path: Full project path.
        mr_iid: Merge request IID in the project.
        include_changes: If True, includes a summary of changed files.
        max_changed_files: Maximum number of changed files to return
                           (default 50, max 200).
    """
    try:
        max_changed_files = _limit(max_changed_files, 50, 200)
        project = _client().projects.get(project_path)
        mr = project.mergerequests.get(mr_iid)
        result = {
            "project_path": project.path_with_namespace,
            "merge_request": _merge_request_summary(mr),
            "description": getattr(mr, "description", None),
            "labels": getattr(mr, "labels", []),
            "assignees": [
                _field(user, "username")
                for user in (getattr(mr, "assignees", None) or [])
            ],
            "reviewers": [
                _field(user, "username")
                for user in (getattr(mr, "reviewers", None) or [])
            ],
            "source_project_id": getattr(mr, "source_project_id", None),
            "target_project_id": getattr(mr, "target_project_id", None),
            "squash": getattr(mr, "squash", None),
            "work_in_progress": getattr(mr, "work_in_progress", None),
        }
        if include_changes:
            if hasattr(mr, "changes"):
                changes_payload = mr.changes()
                changes = changes_payload.get("changes", [])
            else:
                changes = [
                    {
                        "old_path": getattr(diff, "old_path", None),
                        "new_path": getattr(diff, "new_path", None),
                        "new_file": getattr(diff, "new_file", None),
                        "renamed_file": getattr(diff, "renamed_file", None),
                        "deleted_file": getattr(diff, "deleted_file", None),
                    }
                    for diff in mr.diffs.list(per_page=max_changed_files)
                ]
            result["changes_count"] = len(changes)
            result["returned_changes_count"] = min(len(changes), max_changed_files)
            result["changed_files"] = [
                {
                    "old_path": change.get("old_path"),
                    "new_path": change.get("new_path"),
                    "new_file": change.get("new_file"),
                    "renamed_file": change.get("renamed_file"),
                    "deleted_file": change.get("deleted_file"),
                }
                for change in changes[:max_changed_files]
            ]
        return result
    except gitlab.exceptions.GitlabError as e:
        return {"error": f"GitLab API error: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


# ----------------------------------------------------------------------
# Tool: releases, tags, environments, deployments (read-only)
# ----------------------------------------------------------------------

def list_tags(project_path: str, search: Optional[str] = None, limit: int = 30) -> dict:
    """
    Lists recent tags for a project.

    Args:
        project_path: Full project path.
        search: Optional tag name filter.
        limit: Maximum number of results (default 30, max 100).
    """
    try:
        limit = _limit(limit, 30, 100)
        project = _client().projects.get(project_path)
        kwargs = {"per_page": limit, "order_by": "updated", "sort": "desc"}
        if search:
            kwargs["search"] = search
        tags = project.tags.list(**kwargs)
        return {
            "project_path": project.path_with_namespace,
            "count": len(tags),
            "tags": [_tag_summary(tag) for tag in tags],
        }
    except gitlab.exceptions.GitlabError as e:
        return {"error": f"GitLab API error: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


def list_environments(project_path: str, limit: int = 20) -> dict:
    """
    Lists GitLab environments for a project with the latest known deployment.

    Args:
        project_path: Full project path.
        limit: Maximum number of environments to return (default 20, max 100).
    """
    try:
        limit = _limit(limit, 20, 100)
        project = _client().projects.get(project_path)
        environments = project.environments.list(per_page=limit)
        items = []
        for environment in environments:
            latest = None
            try:
                deployments = project.deployments.list(
                    environment=environment.name,
                    order_by="updated_at",
                    sort="desc",
                    per_page=1,
                )
                if deployments:
                    latest = _deployment_summary(deployments[0])
            except gitlab.exceptions.GitlabError:
                latest = None
            items.append(_environment_summary(environment, latest))
        return {
            "project_path": project.path_with_namespace,
            "count": len(items),
            "environments": items,
        }
    except gitlab.exceptions.GitlabError as e:
        return {"error": f"GitLab API error: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


def get_environment_deployments(
    project_path: str,
    environment: str,
    limit: int = 10,
) -> dict:
    """
    Lists recent deployments for an environment.

    Args:
        project_path: Full project path.
        environment: GitLab environment name (for example production, staging).
        limit: Maximum number of deployments to return (default 10, max 50).
    """
    try:
        limit = _limit(limit, 10, 50)
        project = _client().projects.get(project_path)
        deployments = project.deployments.list(
            environment=environment,
            order_by="updated_at",
            sort="desc",
            per_page=limit,
        )
        return {
            "project_path": project.path_with_namespace,
            "environment": environment,
            "count": len(deployments),
            "deployments": [_deployment_summary(deployment) for deployment in deployments],
        }
    except gitlab.exceptions.GitlabError as e:
        return {"error": f"GitLab API error: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


def get_project_ci_overview(
    project_path: str,
    pipeline_limit: int = 5,
    mr_limit: int = 10,
    tag_limit: int = 10,
    environment_limit: int = 10,
) -> dict:
    """
    Returns a compact CI/CD view for a GitLab project.

    Includes project metadata, recent pipelines, failed jobs from the latest
    failed pipeline, open MRs, recent tags, and environments with latest known
    deployments. Use this tool for generic questions like "how is this repo
    doing?" or "give me a CI/CD overview".

    Args:
        project_path: Full project path.
        pipeline_limit: Maximum number of recent pipelines (default 5, max 20).
        mr_limit: Maximum number of open MRs (default 10, max 50).
        tag_limit: Maximum number of recent tags (default 10, max 50).
        environment_limit: Maximum number of environments (default 10, max 50).
    """
    try:
        pipeline_limit = _limit(pipeline_limit, 5, 20)
        mr_limit = _limit(mr_limit, 10, 50)
        tag_limit = _limit(tag_limit, 10, 50)
        environment_limit = _limit(environment_limit, 10, 50)

        project = _client().projects.get(project_path)
        default_branch = getattr(project, "default_branch", None)

        pipeline_kwargs = {
            "per_page": pipeline_limit,
            "order_by": "updated_at",
            "sort": "desc",
        }
        if default_branch:
            pipeline_kwargs["ref"] = default_branch
        pipelines = project.pipelines.list(**pipeline_kwargs)

        failed_pipeline = None
        failed_jobs = []
        for pipeline_ref in pipelines:
            if getattr(pipeline_ref, "status", None) != "failed":
                continue
            pipeline = project.pipelines.get(pipeline_ref.id)
            jobs = pipeline.jobs.list(all=True)
            failed_jobs = [
                _job_summary(job)
                for job in jobs
                if job.status in _FAILED_JOB_STATUSES
            ]
            failed_pipeline = _pipeline_summary(pipeline)
            break

        mr_kwargs = {
            "state": "opened",
            "per_page": mr_limit,
            "order_by": "updated_at",
            "sort": "desc",
        }
        if default_branch:
            mr_kwargs["target_branch"] = default_branch
        merge_requests = project.mergerequests.list(**mr_kwargs)
        tags = project.tags.list(per_page=tag_limit, order_by="updated", sort="desc")
        environments = project.environments.list(per_page=environment_limit)

        environment_items = []
        for environment in environments:
            latest = None
            try:
                deployments = project.deployments.list(
                    environment=environment.name,
                    order_by="updated_at",
                    sort="desc",
                    per_page=1,
                )
                if deployments:
                    latest = _deployment_summary(deployments[0])
            except gitlab.exceptions.GitlabError:
                latest = None
            environment_items.append(_environment_summary(environment, latest))

        return {
            "project": _project_summary(project),
            "default_branch": default_branch,
            "recent_pipelines": [_pipeline_summary(p) for p in pipelines],
            "latest_failed_pipeline": failed_pipeline,
            "latest_failed_pipeline_jobs": failed_jobs,
            "open_merge_requests": [
                _merge_request_summary(mr) for mr in merge_requests
            ],
            "recent_tags": [_tag_summary(tag) for tag in tags],
            "environments": environment_items,
        }
    except gitlab.exceptions.GitlabError as e:
        return {"error": f"GitLab API error: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


# ----------------------------------------------------------------------
# Tool: trigger deployment pipeline (DESTRUCTIVE: requires approval)
# ----------------------------------------------------------------------

# Environments that require explicit approval before triggering
_PROTECTED_ENVIRONMENTS = {"production", "prod", "staging-eu"}


def trigger_deployment_pipeline(
    project_path: str,
    environment: str,
    service: str,
    version: str,
    confirmed: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    Triggers a parameterized deployment pipeline.

    For protected environments (production, etc.), the first invocation returns
    'pending_approval' without executing anything. The agent must then emit a
    request_approval() to the user, and only after confirmation call this tool
    again with confirmed=True.

    Args:
        project_path: GitLab path of the project containing the pipeline.
        environment: Target environment (for example 'staging', 'production').
        service: Name of the service to deploy.
        version: Version/tag/SHA to deploy.
        confirmed: If True, triggers even for protected environments.
                   Set by the agent only after user approval.
    """
    is_protected = environment.lower() in _PROTECTED_ENVIRONMENTS

    if is_protected and not confirmed:
        # Store the intent in session state for audit trail.
        if tool_context is not None:
            tool_context.state["pending_action"] = {
                "type": "trigger_deployment_pipeline",
                "project_path": project_path,
                "environment": environment,
                "service": service,
                "version": version,
            }
        return {
            "status": "pending_approval",
            "reason": f"Deployment to protected environment '{environment}' requires explicit user approval.",
            "intended_action": {
                "project_path": project_path,
                "environment": environment,
                "service": service,
                "version": version,
            },
        }

    try:
        project = _client().projects.get(project_path)
        # Parameterized pipeline: pass variables read by .gitlab-ci.yml.
        pipeline = project.pipelines.create(
            {
                "ref": "main",
                "variables": [
                    {"key": "DEPLOY_ENV", "value": environment},
                    {"key": "DEPLOY_SERVICE", "value": service},
                    {"key": "DEPLOY_VERSION", "value": version},
                ],
            }
        )
        # Clear pending_action sentinel. ADK State has no __delitem__,
        # so we set None and treat None as "no pending action".
        if tool_context is not None and tool_context.state.get("pending_action") is not None:
            tool_context.state["pending_action"] = None
        return {
            "status": "triggered",
            "pipeline_id": pipeline.id,
            "web_url": pipeline.web_url,
            "ref": pipeline.ref,
        }
    except gitlab.exceptions.GitlabError as e:
        return {"error": f"GitLab API error: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


# ----------------------------------------------------------------------
# Tool sentinel: request_approval
# ----------------------------------------------------------------------

def request_approval(action: str, params: dict, reason: str) -> dict:
    """
    Sentinel tool that does NOT execute real actions: its purpose is to make
    the model emit a function call with the standard name 'request_approval',
    which the Open WebUI Pipeline recognizes and renders as an approval UI
    block.

    The agent must call this tool whenever another tool returned
    status='pending_approval'.

    Args:
        action: Human-readable action name (for example
                'trigger_deployment_pipeline').
        params: Complete parameters for the action that will be executed if
                approved.
        reason: Explanation of why approval is required.
    """
    # The Pipeline does not expect this tool to be actually executed:
    # approval rendering happens on the Pipeline side by intercepting the
    # function call. If the flow executes it for any reason, return a readable
    # payload.
    return {
        "approval_pending": True,
        "action": action,
        "params": params,
        "reason": reason,
        "instruction_for_user": "Reply 'approve' to proceed or 'cancel' to abort.",
    }
