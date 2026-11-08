#!/usr/bin/env python3
"""Canvas LMS MCP server.

A local (stdio) Model Context Protocol server that gives Claude access to a
Canvas LMS account via the Canvas REST API.

Configuration is via environment variables:
    CANVAS_API_TOKEN   (required)  Canvas personal access token.
    CANVAS_BASE_URL    (optional)  Canvas instance root, e.g.
                                   https://ateneo.instructure.com
                                   Defaults to the Ateneo de Manila instance.

Most tools are read-only (profile, courses, assignments, grades, announcements,
modules, syllabus, pages, files, discussions, quizzes, calendar, to-do).

A small set of tools WRITE to Canvas: submit an assignment, post a discussion
entry, reply to an entry, comment on a submission, and create a planner note.
Every write tool is gated behind a `confirm` flag that defaults to False. When
`confirm` is False the tool sends nothing — it returns a preview of exactly what
it would do. The write only happens on a second call with `confirm=True`. This
makes it impossible for a single ambiguous tool call to submit or post something
under your name by accident. No tool deletes anything.
"""

import html
import json
import mimetypes
import os
import re
import argparse
import asyncio
import functools
import inspect
import weakref
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Optional, Union
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from a .env next to this file.

    Lets the server find its own configuration regardless of which MCP client
    launched it (Codex, Claude Desktop, Claude Cowork, plain CLI), instead of
    depending on each client's per-server env block. Existing environment
    variables always win, so a client-provided value still overrides the file.
    """
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


# Load file-backed configuration before deriving any module-level settings.
_load_dotenv()

# --------------------------------------------------------------------------- #
# Configuration & constants
# --------------------------------------------------------------------------- #

DEFAULT_BASE_URL = "https://ateneo.instructure.com"
CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
CANVAS_API_TOKEN = os.environ.get("CANVAS_API_TOKEN", "").strip()
API_BASE = f"{CANVAS_BASE_URL}/api/v1"

# Connecting (DNS lookup + TLS) gets 10 s: when the network's DNS is down the
# lookup just hangs, and failing fast with a clear message beats 30 s of silence.
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_PAGES = 20            # safety cap when following pagination links
DEFAULT_PER_PAGE = 100
HTML_TEXT_LIMIT = 4000    # truncate long HTML bodies (syllabus, descriptions)
SWEEP_CONCURRENCY = 8     # cap on simultaneous requests in cross-course sweeps
                          # (overdue/graded-discussion helpers), to avoid
                          # bursting Canvas's per-token rate limit on an
                          # account with many active courses/discussions

LOCAL_TZ_NAME = os.environ.get("CANVAS_TIMEZONE", "Asia/Manila")
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
LOCAL_TZ_LABEL = os.environ.get("CANVAS_TZ_LABEL", "PHT")

mcp = FastMCP("canvas_mcp")


# --------------------------------------------------------------------------- #
# Shared infrastructure
# --------------------------------------------------------------------------- #

class ResponseFormat(str, Enum):
    """Output format for tool responses."""
    MARKDOWN = "markdown"
    JSON = "json"


class CanvasError(Exception):
    """Raised when the Canvas API returns an error or is misconfigured."""

# Some hosts (notably Claude Desktop/Cowork) launch local MCP servers with
# HTTP_PROXY/HTTPS_PROXY pointing at an allow-listing egress proxy. Canvas is
# not on that allow-list, so every request comes back as an opaque HTTP 406 that
# looks like a Canvas error but never touched Canvas. httpx picks those proxy
# variables up automatically via trust_env, so we default to ignoring them and
# talking to Canvas directly. Set CANVAS_TRUST_ENV=1 if you genuinely need the
# proxy (e.g. a corporate network where direct egress is blocked).
CANVAS_TRUST_ENV = os.environ.get("CANVAS_TRUST_ENV", "0").lower() in {"1", "true", "yes"}

USER_AGENT = os.environ.get("CANVAS_USER_AGENT", "canvas-mcp/1.0 (+httpx)")

DEFAULT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": USER_AGENT,
}


def _canvas_client(**kwargs: Any) -> httpx.AsyncClient:
    """httpx.AsyncClient preconfigured for Canvas.

    Centralises proxy handling and default headers so every call site behaves
    the same way.
    """
    kwargs.setdefault("trust_env", CANVAS_TRUST_ENV)
    headers = dict(DEFAULT_HEADERS)
    headers.update(kwargs.pop("headers", None) or {})
    kwargs["headers"] = headers
    return httpx.AsyncClient(**kwargs)


def _headers() -> dict[str, str]:
    if not CANVAS_API_TOKEN:
        raise CanvasError(
            "CANVAS_API_TOKEN is not set. Add your Canvas personal access token "
            "as the CANVAS_API_TOKEN environment variable in your MCP server config."
        )
    return {"Authorization": f"Bearer {CANVAS_API_TOKEN}"}


def _handle_api_error(e: Exception) -> str:
    """Consistent, actionable error formatting across all tools."""
    if isinstance(e, CanvasError):
        return f"Error: {e}"
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 401:
            return ("Error: Unauthorized (401). The Canvas token is missing, expired, "
                    "or was revoked. Generate a new token in Canvas → Account → "
                    "Settings → Approved Integrations → New Access Token.")
        if code == 403:
            return ("Error: Forbidden (403). The token is valid but lacks permission "
                    "for this resource, or a rate limit was hit. Try again shortly.")
        if code == 404:
            return ("Error: Not found (404). Check that the course/assignment ID is "
                    "correct and that you are enrolled in that course.")
        if code == 429:
            return "Error: Rate limit exceeded (429). Wait a moment and retry."
        detail = ""
        try:
            body = (e.response.text or "").strip()
            if body:
                detail = f" Response: {body[:300]}"
        except Exception:
            pass
        if code == 406:
            return ("Error: Canvas API request failed with HTTP 406. This is "
                    "almost always an egress proxy rejecting the request rather "
                    "than Canvas itself — check HTTP_PROXY/HTTPS_PROXY in the "
                    "environment this server was launched in, or set "
                    f"CANVAS_TRUST_ENV=0 to bypass it.{detail}")
        return f"Error: Canvas API request failed with HTTP {code}.{detail}"
    if isinstance(e, httpx.ConnectTimeout):
        return (f"Error: Could not reach {CANVAS_BASE_URL} within 10 s. This computer's "
                "internet or DNS is not answering (not a Canvas or server problem); "
                "retrying will not help until the network recovers.")
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request to Canvas timed out. Check your connection and retry."
    if isinstance(e, httpx.ConnectError):
        if CANVAS_TRUST_ENV:
            return (f"Error: Could not connect to {CANVAS_BASE_URL} while proxy "
                    "environment variables are enabled. The configured "
                    "HTTP_PROXY/HTTPS_PROXY may be unreachable or may reject "
                    "Canvas; set CANVAS_TRUST_ENV=0 to bypass it.")
        return (f"Error: Could not connect to {CANVAS_BASE_URL}. Verify CANVAS_BASE_URL "
                "is your institution's Canvas address.")
    return f"Error: Unexpected {type(e).__name__}: {e}"


_api_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = \
    weakref.WeakKeyDictionary()


def _api_client() -> httpx.AsyncClient:
    """Shared keep-alive client for Canvas API calls (one per event loop), so
    requests reuse the TLS connection instead of opening a new one each time —
    about 40% faster per request. Download/upload clients stay separate."""
    loop = asyncio.get_running_loop()
    client = _api_clients.get(loop)
    if client is None or client.is_closed:
        client = _api_clients[loop] = _canvas_client(timeout=REQUEST_TIMEOUT)
    return client


async def _dns_ready() -> None:
    """Look up the Canvas host with a 10 s cap. httpx resolves in a thread that
    can't be cancelled, so a hung DNS server otherwise stalls each call ~30 s."""
    host = httpx.URL(CANVAS_BASE_URL).host
    try:
        await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, 443), 10)
    except (asyncio.TimeoutError, OSError) as e:
        raise httpx.ConnectTimeout(f"DNS lookup for {host} failed: {e!r}") from None


async def _get(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    """Single GET request returning parsed JSON. `path` is relative to API_BASE."""
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    await _dns_ready()
    resp = await _api_client().get(url, headers=_headers(), params=params)
    resp.raise_for_status()
    return resp.json()


async def _paginated_get(path: str, params: Optional[dict[str, Any]] = None,
                         max_items: int = 500) -> list[Any]:
    """GET that follows Canvas Link-header pagination and concatenates list results."""
    params = dict(params or {})
    params.setdefault("per_page", DEFAULT_PER_PAGE)
    url: Optional[str] = f"{API_BASE}{path}"
    out: list[Any] = []
    pages = 0
    await _dns_ready()
    client = _api_client()
    while url and pages < MAX_PAGES and len(out) < max_items:
        resp = await client.get(url, headers=_headers(),
                                params=params if pages == 0 else None)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            out.extend(data)
        else:
            return data  # non-list payload; return as-is
        url = _next_link(resp.headers.get("link", ""))
        pages += 1
    return out[:max_items]


def _next_link(link_header: str) -> Optional[str]:
    """Parse the `next` URL from a Canvas RFC-5988 Link header."""
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().strip("<>")
        if any('rel="next"' in s.replace(" ", "") or "rel=next" in s.replace(" ", "")
               for s in section[1:]):
            return url
    return None


def _strip_html(raw: Optional[str], limit: int = HTML_TEXT_LIMIT) -> str:
    """Convert a Canvas HTML body to readable plain text."""
    if not raw:
        return ""
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", raw)
    text = re.sub(r"(?i)</\s*(p|div|li|h[1-6]|tr)\s*>", "\n", text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n…[truncated]"
    return text


def _fmt_dt(value: Optional[str]) -> str:
    """Format an ISO-8601 UTC timestamp from Canvas into local account time."""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        local = dt.astimezone(LOCAL_TZ)
        return f"{local.strftime('%Y-%m-%d %H:%M')} {LOCAL_TZ_LABEL}"
    except (ValueError, AttributeError):
        return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Internal reuse: fetch a normalized course list + an id→name map ------------- #

async def _fetch_courses(enrollment_state: str = "active") -> list[dict[str, Any]]:
    params = {
        "enrollment_state": enrollment_state,
        "include[]": ["term", "total_scores"],
        "state[]": ["available", "completed"],
    }
    raw = await _paginated_get("/courses", params)
    courses = []
    for c in raw:
        if not isinstance(c, dict) or "id" not in c:
            continue
        if c.get("access_restricted_by_date"):
            continue
        courses.append(c)
    return courses


def _course_name(c: dict[str, Any]) -> str:
    return c.get("name") or c.get("course_code") or f"Course {c.get('id')}"


async def _gather_limited(coros, limit: int = SWEEP_CONCURRENCY):
    """Like asyncio.gather(*coros, return_exceptions=True), but caps how many
    run at once — an unbounded fan-out across many courses/topics can burst
    past Canvas's per-token rate limit."""
    sem = asyncio.Semaphore(limit)

    async def _bounded(coro):
        async with sem:
            return await coro

    return await asyncio.gather(*(_bounded(c) for c in coros), return_exceptions=True)


async def _fetch_overdue_assignments(
    courses: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Cross-course sweep for assignments that are overdue and not submitted.

    Deliberately does NOT rely on Canvas's server-side bucket=overdue filter
    (it has been observed to silently drop on-paper submission types with no
    online submission). Instead fetches each active course's full assignment
    list with submission data and filters client-side.

    Returns (rows, skipped_course_names) — one broken course must not fail the
    whole sweep, but the caller must be able to tell the user it was skipped.
    """
    now = _now()
    if courses is None:
        courses = await _fetch_courses(enrollment_state="active")
    out: list[dict[str, Any]] = []
    skipped: list[str] = []
    results = await _gather_limited(
        _paginated_get(f"/courses/{c['id']}/assignments",
                       {"include[]": ["submission"]}) for c in courses)
    for c, items in zip(courses, results):
        if isinstance(items, BaseException):
            skipped.append(_course_name(c))
            continue  # one broken course must not fail the whole sweep
        for a in items:
            if not isinstance(a, dict):
                continue
            due = a.get("due_at")
            if not due:
                continue
            try:
                due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if due_dt >= now:
                continue
            sub = a.get("submission") or {}
            state = sub.get("workflow_state", "unsubmitted")
            submitted = bool(sub.get("submitted_at"))
            missing = bool(sub.get("missing"))
            excused = bool(sub.get("excused"))
            if excused or submitted:
                continue
            if not (missing or state == "unsubmitted"):
                continue
            out.append({
                "id": a.get("id"),
                "due": due, "type": "assignment",
                "title": a.get("name") or "(untitled)",
                "course_id": c["id"], "context": _course_name(c),
                "points": a.get("points_possible"),
                "status": "missing" if missing else "not submitted",
                "html_url": a.get("html_url"),
            })
    return out, skipped


async def _fetch_graded_discussions(
    courses: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Cross-course sweep for graded discussion topics (lock_at/due_at set),
    since Canvas's planner and todo endpoints do not reliably surface them.

    Returns (rows, skipped_course_names). A course whose topic list fails to
    fetch is reported as skipped; a topic whose assignment detail fails only
    loses its submission status ("—") and is NOT counted as a skipped course.
    """
    if courses is None:
        courses = await _fetch_courses(enrollment_state="active")
    out: list[dict[str, Any]] = []
    skipped: list[str] = []
    results = await _gather_limited(
        _paginated_get(f"/courses/{c['id']}/discussion_topics", {})
        for c in courses)

    # Collect every actionable topic first, so the per-topic assignment detail
    # fetches can be issued concurrently instead of one-at-a-time (N+1).
    pending: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]] = []
    for c, topics in zip(courses, results):
        if isinstance(topics, BaseException):
            skipped.append(_course_name(c))
            continue
        for t in topics:
            if not isinstance(t, dict):
                continue
            assignment = t.get("assignment") or {}
            due = assignment.get("due_at") or t.get("lock_at")
            if not (assignment.get("id") or t.get("lock_at")):
                continue  # not graded and not time-boxed — not actionable
            if not due:
                continue
            pending.append((c, t, assignment, due))

    graded_idx = [i for i, (_c, _t, assignment, _due) in enumerate(pending)
                  if assignment.get("id")]
    detail_results = await _gather_limited(
        _get(f"/courses/{pending[i][0]['id']}/assignments"
             f"/{pending[i][2]['id']}", {"include[]": ["submission"]})
        for i in graded_idx)
    details = dict(zip(graded_idx, detail_results))

    for i, (c, t, assignment, due) in enumerate(pending):
        status = "—"
        a = details.get(i)
        if isinstance(a, dict):
            sub = a.get("submission") or {}
            if sub.get("submitted_at"):
                status = "submitted"
            elif sub.get("missing"):
                status = "missing"
            else:
                status = "not submitted"
        out.append({
            "id": assignment.get("id") or t.get("id"),
            "due": due, "type": "discussion_topic",
            "title": t.get("title") or "(untitled)",
            "course_id": c["id"], "context": _course_name(c),
            "points": assignment.get("points_possible"),
            "status": status,
            "html_url": t.get("html_url"),
        })
    return out, skipped


# --------------------------------------------------------------------------- #
# Course references — every course_id accepts an ID or a code/name ("THEO 13")
# --------------------------------------------------------------------------- #

CourseRef = Union[
    Annotated[int, Field(ge=1)],
    Annotated[str, Field(min_length=1,
                         description="Course code or name, e.g. \"THEO 13\".")],
]
COURSE_DESC = "Canvas course ID, or a course code/name such as \"THEO 13\"."
COURSE_CACHE_TTL = 600.0  # seconds; course lists rarely change mid-session
_course_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _tokens(text: str) -> list[str]:
    """'THEO 13-L1' -> ['theo', '13', 'l', '1']; letters and digits split apart."""
    return re.findall(r"[a-z]+|\d+", text.lower())


def _match_courses(ref: str, courses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exact code/name matches if any, else courses containing every query token
    (digits must match whole, so "13" never matches "131"; words match as prefixes)."""
    q = _tokens(ref)
    if not q:
        return []

    def fields(c: dict[str, Any]) -> list[list[str]]:
        code = c.get("course_code") or ""
        return [_tokens(code.split("-")[0]), _tokens(code), _tokens(c.get("name") or "")]

    exact = [c for c in courses if q in fields(c)]
    if exact:
        return exact
    return [c for c in courses
            if all(any(t == w if w.isdigit() else t.startswith(w)
                       for t in sum(fields(c)[1:], [])) for w in q)]


async def _resolve_course(ref: Any) -> int:
    if isinstance(ref, int):
        return ref
    ref = str(ref).strip()
    if ref.isdigit():
        return int(ref)
    for state in ("active", "completed"):
        hit = _course_cache.get(state)
        if not hit or asyncio.get_running_loop().time() - hit[0] > COURSE_CACHE_TTL:
            hit = (asyncio.get_running_loop().time(), await _fetch_courses(state))
            _course_cache[state] = hit
        matches = _match_courses(ref, hit[1])
        if len(matches) == 1:
            return int(matches[0]["id"])
        if matches:
            names = "; ".join(f"{c.get('course_code')} — {_course_name(c)} (ID {c['id']})"
                              for c in matches[:8])
            raise CanvasError(f"{ref!r} matches {len(matches)} courses: {names}. "
                              "Use the ID or a more specific code/name.")
    raise CanvasError(f"No course matches {ref!r}. Use canvas_list_courses to see "
                      "course codes, names, and IDs.")


async def _resolve_course_fields(params: BaseModel) -> None:
    for field in ("course_id", "course_ids", "exclude_course_ids"):
        value = getattr(params, field, None)
        if isinstance(value, list):
            setattr(params, field, [await _resolve_course(v) for v in value])
        elif value is not None:
            setattr(params, field, await _resolve_course(value))


# CANVAS_TOOLSET=lean exposes only canvas_run_code (read-only) plus the
# individual write tools, cutting the tool definitions every chat loads from
# ~15k to ~3k tokens. Scripts can still call every read tool via canvas.tool().
CANVAS_TOOLSET = os.environ.get("CANVAS_TOOLSET", "full").strip().lower()

# Every tool, exposed over MCP or not: name -> (wrapper, read_only).
_TOOLS: dict[str, tuple[Any, bool]] = {}


def _model_description(doc: Optional[str]) -> str:
    """Docstring minus its Args:/Returns: sections — the input schema already
    describes each parameter, so repeating them only costs the model tokens."""
    return re.split(r"\n\s*(?:Args|Returns):\s*\n", inspect.cleandoc(doc or ""))[0].strip()


def _tool(**kwargs: Any):
    """mcp.tool, plus course-name resolution before the tool body runs."""
    def register(fn):
        @functools.wraps(fn)
        async def wrapper(params):
            try:
                await _resolve_course_fields(params)
            except Exception as e:  # noqa: BLE001
                return _handle_api_error(e)
            return await fn(params)
        name = kwargs["name"]
        read_only = bool(kwargs.get("annotations", {}).get("readOnlyHint"))
        _TOOLS[name] = (wrapper, read_only)
        lean_keep = name == "canvas_run_code" or not (read_only or name == "canvas_run_code_write")
        if CANVAS_TOOLSET != "lean" or lean_keep:
            mcp.tool(description=_model_description(fn.__doc__), **kwargs)(wrapper)
        return wrapper
    return register


# --------------------------------------------------------------------------- #
# Input models
# --------------------------------------------------------------------------- #

def _drop_titles(schema: dict[str, Any]) -> None:
    """Auto-generated titles ("Course Id") just repeat field names; the model
    pays tokens for every one on every chat."""
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)


class _Base(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True,
                              extra="forbid", json_schema_extra=_drop_titles)


class ProfileInput(_Base):
    pass


class ListCoursesInput(_Base):
    enrollment_state: str = Field(
        default="active",
        description="Which courses to list: 'active' (current), 'completed' (past), "
                    "or 'all'.",
        pattern=r"^(active|completed|all)$",
    )
    current_term_only: bool = Field(
        default=False,
        description="When True, exclude courses whose term has already ended or "
                    "not yet started (e.g. old PE courses from a prior school "
                    "year). Courses with no term date bounds (permanent hub "
                    "sites like a student-conduct or orientation site) are NOT "
                    "filtered by this flag since there's no date to judge them "
                    "by — use term_name to exclude those explicitly.",
    )
    term_name: Optional[str] = Field(
        default=None,
        description="Case-insensitive substring filter on the course's term "
                    "name (e.g. 'AY 2025-2026 Sem 1'). Use this to pin an exact "
                    "term when current_term_only isn't precise enough.",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' for a readable summary or 'json' for raw structured data.",
    )


class ListAssignmentsInput(_Base):
    course_id: CourseRef = Field(..., description="Canvas course ID (from canvas_list_courses).")
    bucket: Optional[str] = Field(
        default=None,
        description="Optional filter: 'upcoming', 'past', 'overdue', 'unsubmitted', "
                    "'ungraded', 'undated', or 'future'. Omit for all assignments. "
                    "'unsubmitted' is filtered locally (Canvas's own bucket=unsubmitted "
                    "drops on-paper submission types), so it also catches assignments "
                    "with no online submission method.",
        pattern=r"^(upcoming|past|overdue|unsubmitted|ungraded|undated|future)$",
    )
    include_submission: bool = Field(
        default=True,
        description="Include the user's submission status and score for each assignment.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class GetAssignmentInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    assignment_id: int = Field(..., description="Canvas assignment ID.", ge=1)


class UpcomingInput(_Base):
    days_ahead: int = Field(
        default=14,
        description="How many days forward to look for due items across all courses. "
                    "Overdue, unsubmitted items are always included regardless of "
                    "this window.",
        ge=1, le=90,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class GradesInput(_Base):
    course_id: Optional[CourseRef] = Field(
        default=None,
        description="Optional course ID. If omitted, returns current grade summary "
                    "across all active courses. If given, returns per-assignment scores.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class AnnouncementsInput(_Base):
    course_id: Optional[CourseRef] = Field(
        default=None,
        description="Optional course ID. If omitted, pulls announcements from all "
                    "active courses.",
    )
    course_ids: Optional[list[CourseRef]] = Field(
        default=None,
        description="Optional list of specific course IDs to pull announcements "
                    "from. When given, this takes precedence over course_id and "
                    "over sweeping all active courses.",
    )
    exclude_course_ids: Optional[list[CourseRef]] = Field(
        default=None,
        description="Course IDs to exclude from the sweep — useful for "
                    "permanent hub/orientation sites that generate noisy "
                    "unrelated announcements (weather notices, etc.) when you "
                    "sweep all active courses.",
    )
    days_back: int = Field(default=21, description="How many days back to look.",
                           ge=1, le=180)
    max_body_chars: int = Field(
        default=1500, ge=100, le=8000,
        description="Max characters of each announcement body to include "
                    "before truncating. Lower this to shrink response size when "
                    "sweeping many courses; raise it if bodies are getting cut "
                    "off mid-sentence and you need the full text.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class ModulesInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    include_items: bool = Field(
        default=True,
        description="Include the items (pages, assignments, files) inside each module.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class SyllabusInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

def _submission_summary(sub: Optional[dict[str, Any]]) -> str:
    if not sub:
        return "no submission data"
    state = sub.get("workflow_state", "unsubmitted")
    score = sub.get("score")
    bits = [state]
    if sub.get("submitted_at"):
        bits.append(f"submitted {_fmt_dt(sub['submitted_at'])}")
    if score is not None:
        bits.append(f"score {score}")
    if sub.get("late"):
        bits.append("LATE")
    if sub.get("missing"):
        bits.append("MISSING")
    return ", ".join(bits)


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@_tool(
    name="canvas_get_profile",
    annotations={"title": "Get Canvas Profile", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_get_profile(params: ProfileInput) -> str:
    """Return the authenticated user's Canvas profile. Use this to verify the token
    works and to confirm whose account is connected.

    Returns:
        str: Markdown with name, login id, primary email, and Canvas user ID.
    """
    try:
        p = await _get("/users/self/profile")
        return (f"**Connected to Canvas** ({CANVAS_BASE_URL})\n\n"
                f"- Name: {p.get('name', '—')}\n"
                f"- Login: {p.get('login_id', '—')}\n"
                f"- Email: {p.get('primary_email', '—')}\n"
                f"- User ID: {p.get('id', '—')}\n"
                f"- Time zone: {p.get('time_zone', '—')}")
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_list_courses",
    annotations={"title": "List Courses", "readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_courses(params: ListCoursesInput) -> str:
    """List the user's Canvas courses with term and current grade where available.

    Args:
        params.enrollment_state: 'active', 'completed', or 'all'.
        params.current_term_only: When True, drop courses whose term has already
            ended or has not yet started (e.g. old PE courses from a prior school
            year). Courses whose term has no start/end dates — permanent hub sites
            like a student-conduct or orientation site — are left in, since there
            is no date to judge them by; use params.term_name for those.
        params.term_name: Case-insensitive substring filter on the course's term
            name (e.g. 'AY 2025-2026 Sem 1') to pin an exact term.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: For each course — id, name, course code, term, and current score/grade.
    """
    try:
        state = "active" if params.enrollment_state == "all" else params.enrollment_state
        courses = await _fetch_courses(enrollment_state=state)
        if params.enrollment_state == "all":
            done = await _fetch_courses(enrollment_state="completed")
            seen = {c["id"] for c in courses}
            courses.extend(c for c in done if c["id"] not in seen)

        if params.current_term_only:
            now = _now()

            def _in_current_term(c: dict[str, Any]) -> bool:
                term = c.get("term") or {}
                start, end = term.get("start_at"), term.get("end_at")
                try:
                    if start:
                        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                        if now < start_dt:
                            return False
                    if end:
                        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                        if now > end_dt:
                            return False
                except (ValueError, AttributeError):
                    pass
                return True

            courses = [c for c in courses if _in_current_term(c)]

        if params.term_name:
            needle = params.term_name.lower()
            courses = [c for c in courses
                       if needle in ((c.get("term") or {}).get("name", "") or "").lower()]

        if params.response_format == ResponseFormat.JSON:
            slim = [{
                "id": c["id"], "name": _course_name(c),
                "course_code": c.get("course_code"),
                "term": (c.get("term") or {}).get("name"),
                "current_score": c.get("computed_current_score"),
                "current_grade": c.get("computed_current_grade"),
            } for c in courses]
            return json.dumps({"count": len(slim), "courses": slim}, indent=2)

        if not courses:
            return "No courses found for that enrollment state."
        lines = [f"**{len(courses)} course(s)** ({params.enrollment_state}):\n"]
        for c in courses:
            term = (c.get("term") or {}).get("name", "")
            score = c.get("computed_current_score")
            grade = c.get("computed_current_grade")
            grade_str = ""
            if score is not None or grade is not None:
                grade_str = f" — grade: {grade or ''} {f'({score}%)' if score is not None else ''}".rstrip()
            lines.append(f"- **{_course_name(c)}** (ID {c['id']}, {c.get('course_code','')})"
                         f"{f' · {term}' if term else ''}{grade_str}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_list_assignments",
    annotations={"title": "List Assignments", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_assignments(params: ListAssignmentsInput) -> str:
    """List assignments for a course, including due dates and the user's submission
    status/score.

    Args:
        params.course_id: Canvas course ID.
        params.bucket: Optional Canvas bucket filter (upcoming/past/overdue/etc.).
            'unsubmitted' is filtered client-side rather than server-side.
        params.include_submission: Include submission status and score.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Each assignment — id, name, due date, points possible, html_url, and
             (if requested) submission state, score, late/missing flags.
    """
    try:
        # Canvas's own bucket=unsubmitted drops on-paper submission types (assignments
        # with no online submission method), so we filter that one case client-side.
        local_unsubmitted = params.bucket == "unsubmitted"
        api_params: dict[str, Any] = {"order_by": "due_at"}
        if params.bucket and not local_unsubmitted:
            api_params["bucket"] = params.bucket
        if params.include_submission or local_unsubmitted:
            api_params["include[]"] = ["submission"]
        items = await _paginated_get(f"/courses/{params.course_id}/assignments", api_params)
        if local_unsubmitted:
            filtered = []
            for a in items:
                if not isinstance(a, dict):
                    continue
                sub = a.get("submission") or {}
                if sub.get("excused") or sub.get("submitted_at"):
                    continue
                if not (sub.get("missing")
                        or sub.get("workflow_state", "unsubmitted") == "unsubmitted"):
                    continue
                filtered.append(a)
            items = filtered

        if params.response_format == ResponseFormat.JSON:
            slim = [{
                "id": a.get("id"), "name": a.get("name"),
                "due_at": a.get("due_at"),
                "points_possible": a.get("points_possible"),
                "html_url": a.get("html_url"),
                "submission": a.get("submission"),
            } for a in items if isinstance(a, dict)]
            return json.dumps({"course_id": params.course_id, "count": len(slim),
                               "assignments": slim}, indent=2)

        if not items:
            return f"No assignments found for course {params.course_id}" + \
                   (f" (bucket: {params.bucket})." if params.bucket else ".")
        lines = [f"**{len(items)} assignment(s)** in course {params.course_id}"
                 f"{f' (bucket: {params.bucket})' if params.bucket else ''}:\n"]
        for a in items:
            if not isinstance(a, dict):
                continue
            pts = a.get("points_possible")
            line = (f"- **{a.get('name','(untitled)')}** (ID {a.get('id')}) — "
                    f"due {_fmt_dt(a.get('due_at'))}"
                    f"{f', {pts} pts' if pts is not None else ''}")
            if params.include_submission:
                line += f"\n    submission: {_submission_summary(a.get('submission'))}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_get_assignment",
    annotations={"title": "Get Assignment Detail", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_get_assignment(params: GetAssignmentInput) -> str:
    """Get the full detail of one assignment: description, due date, points, allowed
    submission types, rubric criteria, and the user's submission.

    Args:
        params.course_id: Canvas course ID.
        params.assignment_id: Canvas assignment ID.

    Returns:
        str: Markdown detail including the plain-text assignment description.
    """
    try:
        a = await _get(f"/courses/{params.course_id}/assignments/{params.assignment_id}",
                       {"include[]": ["submission"]})
        out = [f"# {a.get('name','(untitled)')}",
               f"- Course: {params.course_id} · Assignment ID: {a.get('id')}",
               f"- Due: {_fmt_dt(a.get('due_at'))}",
               f"- Points possible: {a.get('points_possible', '—')}",
               f"- Submission types: {', '.join(a.get('submission_types', [])) or '—'}",
               f"- Link: {a.get('html_url', '—')}",
               f"- Your submission: {_submission_summary(a.get('submission'))}"]
        rubric = a.get("rubric")
        if rubric:
            # Best-effort: pull the grader's per-criterion scores/comments.
            assessment: dict[str, Any] = {}
            try:
                sub = await _get(
                    f"/courses/{params.course_id}/assignments/"
                    f"{params.assignment_id}/submissions/self",
                    {"include[]": ["rubric_assessment"]})
                assessment = sub.get("rubric_assessment") or {}
            except Exception:  # noqa: BLE001
                assessment = (a.get("submission") or {}).get("rubric_assessment") or {}

            total_pts = sum((c.get("points") or 0) for c in rubric)
            out.append(f"\n## Rubric ({total_pts:g} pts total)")
            for crit in rubric:
                cid = str(crit.get("id", ""))
                out.append(f"\n**{crit.get('description','(criterion)')}** "
                           f"— {crit.get('points','?')} pts")
                long_desc = _strip_html(crit.get("long_description"), limit=600)
                if long_desc:
                    out.append(long_desc)
                ratings = crit.get("ratings") or []
                if ratings:
                    scale = "; ".join(
                        f"{r.get('points','?')}: {r.get('description','')}"
                        for r in ratings)
                    out.append(f"_Scale:_ {scale}")
                got = assessment.get(cid)
                if isinstance(got, dict):
                    score = got.get("points")
                    comment = got.get("comments")
                    bits = []
                    if score is not None:
                        bits.append(f"{score} pts")
                    if comment:
                        bits.append(_strip_html(comment, limit=400))
                    if bits:
                        out.append(f"➤ _Your score:_ {' — '.join(bits)}")
        desc = _strip_html(a.get("description"))
        if desc:
            out.append("\n## Description\n" + desc)
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_list_upcoming",
    annotations={"title": "List Upcoming Due Items", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_upcoming(params: UpcomingInput) -> str:
    """List everything due soon across ALL courses (assignments, quizzes, graded
    discussions, etc.), sorted by due date. This is the 'what do I have coming up'
    view.

    Overdue, still-unsubmitted items are ALWAYS swept in and reported in their own
    section, independent of days_ahead. Graded discussion topics are also swept in,
    because Canvas's planner does not reliably surface them.

    Args:
        params.days_ahead: Look-ahead window in days (1–90). Only affects the
            upcoming section, not the overdue sweep.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Overdue unsubmitted items first, then a consolidated, chronologically
             sorted list of items due in the window — each with course, title, due
             date, points, and submission status.
    """
    try:
        start = _now().date()
        end = start + timedelta(days=params.days_ahead)
        items = await _paginated_get(
            "/planner/items",
            {"start_date": start.isoformat(), "end_date": end.isoformat()},
            max_items=200,
        )
        rows = []
        for it in items:
            if not isinstance(it, dict):
                continue
            pl = it.get("plannable") or {}
            due = it.get("plannable_date") or pl.get("due_at")
            sub = it.get("submissions")
            status = "—"
            if isinstance(sub, dict):
                if sub.get("submitted"):
                    status = "submitted"
                elif sub.get("missing"):
                    status = "missing"
                else:
                    status = "not submitted"
            # Graded discussions are keyed by their backing assignment id
            # everywhere else in this tool (see _fetch_graded_discussions) —
            # match that here so cross-source dedup actually recognizes the
            # same item instead of comparing a topic id against an
            # assignment id.
            row_id = pl.get("id")
            if it.get("plannable_type") == "discussion_topic":
                row_id = pl.get("assignment_id") or row_id
            rows.append({
                "id": row_id,
                "due": due,
                "type": it.get("plannable_type"),
                "title": pl.get("title") or pl.get("name") or "(untitled)",
                "course_id": it.get("course_id"),
                "context": it.get("context_name"),
                "points": pl.get("points_possible"),
                "status": status,
                "html_url": it.get("html_url"),
            })

        def _key(r: dict[str, Any]) -> tuple[Any, Any, Any]:
            rid = r.get("id")
            if rid is not None:
                return (r.get("course_id"), "id", rid)
            return (r.get("course_id"), r.get("title"), r.get("due"))

        def _due_dt(value: Optional[str]) -> Optional[datetime]:
            if not value:
                return None
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return None
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        # Always sweep overdue items — the planner window alone hides them.
        # Both sweeps need the same course list and are independent of each
        # other, so fetch the list once and run them concurrently.
        courses = await _fetch_courses(enrollment_state="active")
        (overdue_raw, overdue_skipped), (discussions, disc_skipped) = \
            await asyncio.gather(_fetch_overdue_assignments(courses),
                                 _fetch_graded_discussions(courses))
        overdue = [r for r in overdue_raw
                   if _key(r) not in {_key(x) for x in rows}]
        skipped = list(dict.fromkeys(overdue_skipped + disc_skipped))

        # Graded discussions: split into overdue vs in-window, merge into both lists.
        now = _now()
        for d in discussions:
            d_dt = _due_dt(d["due"])
            if d_dt is None:
                continue
            if d_dt < now and d["status"] in ("missing", "not submitted"):
                if _key(d) not in {_key(x) for x in rows} | {_key(x) for x in overdue}:
                    overdue.append(d)
            elif start <= d_dt.date() <= end:
                if _key(d) not in {_key(x) for x in rows}:
                    rows.append(d)

        overdue.sort(key=lambda r: r["due"] or "9999")
        rows.sort(key=lambda r: r["due"] or "9999")

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"window_days": params.days_ahead, "count": len(rows),
                               "items": rows, "overdue": overdue,
                               "skipped_courses": skipped}, indent=2)

        def _render(r: dict[str, Any]) -> str:
            pts = f", {r['points']} pts" if r["points"] is not None else ""
            ctx = r["context"] or (f"course {r['course_id']}" if r["course_id"] else "")
            return (f"- **{_fmt_dt(r['due'])}** — {r['title']} "
                    f"[{r['type']}]{pts}\n    {ctx} · {r['status']}")

        note = ""
        if skipped:
            note = (f"_(Note: could not check {', '.join(skipped)} — you may be "
                    f"missing items from "
                    f"{'this course' if len(skipped) == 1 else 'these courses'}.)_")
        if not rows and not overdue:
            empty = (f"Nothing overdue, and nothing due in the next "
                     f"{params.days_ahead} days.")
            return f"{note}\n\n{empty}" if note else empty
        lines: list[str] = []
        if note:
            lines.append(note + "\n")
        if overdue:
            lines.append(f"## ⚠️ Overdue (not submitted)\n"
                         f"**{len(overdue)} item(s) past due:**\n")
            lines.extend(_render(r) for r in overdue)
            lines.append("")
        if rows:
            lines.append(
                f"**{len(rows)} item(s) due in the next {params.days_ahead} days:**\n")
            lines.extend(_render(r) for r in rows)
        else:
            lines.append(f"Nothing else due in the next {params.days_ahead} days.")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


async def _aggregate_course_score(course_id: int) -> Optional[tuple[float, float, int]]:
    """Best-effort fallback grade estimate: sum(earned)/sum(possible) over graded
    submissions only. Returns (earned, possible, graded_count) or None if nothing
    graded yet. This is an unweighted approximation — it ignores assignment group
    weighting — used only when Canvas's own computed course score is hidden."""
    try:
        items = await _paginated_get(
            f"/courses/{course_id}/assignments", {"include[]": ["submission"]})
    except Exception:  # noqa: BLE001
        return None
    earned = possible = 0.0
    n = 0
    for a in items:
        if not isinstance(a, dict):
            continue
        sub = a.get("submission") or {}
        score = sub.get("score")
        pts = a.get("points_possible")
        if score is None or pts is None:
            continue
        earned += score
        possible += pts
        n += 1
    if n == 0 or possible <= 0:
        return None
    return (earned, possible, n)


@_tool(
    name="canvas_get_grades",
    annotations={"title": "Get Grades", "readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": True},
)
async def canvas_get_grades(params: GradesInput) -> str:
    """Get grades. With no course_id: a current-grade summary across all active
    courses. With a course_id: per-assignment scores for that course.

    Args:
        params.course_id: Optional course ID.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Grade summary (per course) or scored assignment list (per course).
             In the summary, courses whose computed score is hidden by the
             instructor show an unweighted estimate derived from graded items.
    """
    try:
        if params.course_id is None:
            courses = await _fetch_courses(enrollment_state="active")
            summary = [{
                "course_id": c["id"], "name": _course_name(c),
                "current_score": c.get("computed_current_score"),
                "current_grade": c.get("computed_current_grade"),
                "estimated_score": None,
                "estimated_from_count": None,
            } for c in courses]
            # Some institutions hide the computed course score; fall back to an
            # unweighted average of whatever individual submissions are graded.
            for s in summary:
                if s["current_score"] is None and s["current_grade"] is None:
                    agg = await _aggregate_course_score(s["course_id"])
                    if agg:
                        earned, possible, n = agg
                        s["estimated_score"] = round(earned / possible * 100, 1)
                        s["estimated_from_count"] = n
            if params.response_format == ResponseFormat.JSON:
                return json.dumps({"count": len(summary), "grades": summary}, indent=2)
            if not summary:
                return "No active courses found."
            lines = ["**Current grades (active courses):**\n"]
            for s in summary:
                sc, gr = s["current_score"], s["current_grade"]
                if sc is not None:
                    val = f"{gr or ''} ({sc}%)"
                elif s["estimated_score"] is not None:
                    val = (f"~{s['estimated_score']:.1f}% (estimated from "
                           f"{s['estimated_from_count']} graded item(s), unweighted)")
                else:
                    val = "no grade posted"
                lines.append(f"- **{s['name']}** (ID {s['course_id']}): {val}")
            return "\n".join(lines)

        # Per-course: assignments with submission scores
        items = await _paginated_get(
            f"/courses/{params.course_id}/assignments", {"include[]": ["submission"]})
        graded = []
        for a in items:
            if not isinstance(a, dict):
                continue
            sub = a.get("submission") or {}
            graded.append({
                "assignment": a.get("name"),
                "score": sub.get("score"),
                "points_possible": a.get("points_possible"),
                "state": sub.get("workflow_state"),
                "late": sub.get("late"), "missing": sub.get("missing"),
            })
        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"course_id": params.course_id, "count": len(graded),
                               "assignments": graded}, indent=2)
        if not graded:
            return f"No assignments found for course {params.course_id}."
        lines = [f"**Scores for course {params.course_id}:**\n"]
        for g in graded:
            sc = g["score"]
            pp = g["points_possible"]
            score_str = f"{sc}/{pp}" if sc is not None else f"—/{pp if pp is not None else '?'}"
            flags = " ".join(f for f, on in
                             (("LATE", g["late"]), ("MISSING", g["missing"])) if on)
            lines.append(f"- {g['assignment']}: {score_str} "
                         f"({g['state'] or 'ungraded'}){f' [{flags}]' if flags else ''}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_list_announcements",
    annotations={"title": "List Announcements", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_announcements(params: AnnouncementsInput) -> str:
    """List recent announcements, either for one course or across all active courses.

    Args:
        params.course_id: Optional course ID (omit for all active courses).
        params.course_ids: Optional explicit list of course IDs. When given it
            takes precedence over params.course_id and over the all-active sweep.
        params.exclude_course_ids: Course IDs to drop from the all-active sweep.
            This is the way to keep permanent hub sites (e.g. a SERVIRE/weather-
            notice site, Student Conduct, Passport to Canvas) out of a broad
            sweep — they post frequent announcements unrelated to coursework.
        params.days_back: How far back to look (1–180 days).
        params.max_body_chars: Max characters of each announcement body before
            truncation (100–8000). Lower it to shrink a wide sweep's response.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Announcements with course, title, posted date, and plain-text body.
    """
    try:
        if params.course_ids:
            context_codes = [f"course_{cid}" for cid in params.course_ids]
            id_to_name = {cid: f"course {cid}" for cid in params.course_ids}
        elif params.course_id is not None:
            context_codes = [f"course_{params.course_id}"]
            id_to_name = {params.course_id: f"course {params.course_id}"}
        else:
            courses = await _fetch_courses(enrollment_state="active")
            context_codes = [f"course_{c['id']}" for c in courses]
            id_to_name = {c["id"]: _course_name(c) for c in courses}
            if params.exclude_course_ids:
                excluded = set(params.exclude_course_ids)
                context_codes = [f"course_{cid}" for cid in id_to_name
                                 if cid not in excluded]
                id_to_name = {cid: name for cid, name in id_to_name.items()
                              if cid not in excluded}
            if not context_codes:
                return "No active courses to read announcements from."

        start = (_now() - timedelta(days=params.days_back)).date().isoformat()
        # Canvas caps context_codes at 10 per request; batch so no course is dropped.
        items: list[Any] = []
        for i in range(0, len(context_codes), 10):
            items.extend(await _paginated_get("/announcements", {
                "context_codes[]": context_codes[i:i + 10],
                "start_date": start,
                "active_only": "true",
            }, max_items=100))

        parsed = []
        for a in items:
            if not isinstance(a, dict):
                continue
            cid = None
            ctx = a.get("context_code", "")
            if ctx.startswith("course_"):
                try:
                    cid = int(ctx.split("_", 1)[1])
                except ValueError:
                    cid = None
            parsed.append({
                "course": id_to_name.get(cid, ctx),
                "title": a.get("title"),
                "posted_at": a.get("posted_at") or a.get("created_at"),
                "url": a.get("html_url"),
                "body": _strip_html(a.get("message"), limit=params.max_body_chars),
            })
        parsed.sort(key=lambda r: r["posted_at"] or "", reverse=True)

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"count": len(parsed), "announcements": parsed}, indent=2)
        if not parsed:
            return f"No announcements in the last {params.days_back} days."
        lines = [f"**{len(parsed)} announcement(s) (last {params.days_back} days):**\n"]
        for a in parsed:
            lines.append(f"### {a['title']}\n"
                         f"_{a['course']} · {_fmt_dt(a['posted_at'])}_\n\n"
                         f"{a['body'] or '(no text)'}\n")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_list_modules",
    annotations={"title": "List Course Modules", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_modules(params: ModulesInput) -> str:
    """List the modules (course structure) for a course, optionally with their items
    such as pages, assignments, files, and external links.

    Args:
        params.course_id: Canvas course ID.
        params.include_items: Include items inside each module.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Modules with state and, if requested, nested items and their URLs.
    """
    try:
        api_params: dict[str, Any] = {}
        if params.include_items:
            api_params["include[]"] = ["items"]
        modules = await _paginated_get(f"/courses/{params.course_id}/modules", api_params)

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"course_id": params.course_id, "count": len(modules),
                               "modules": modules}, indent=2)
        if not modules:
            return f"No modules found for course {params.course_id}."
        lines = [f"**{len(modules)} module(s) in course {params.course_id}:**\n"]
        for m in modules:
            if not isinstance(m, dict):
                continue
            lines.append(f"### {m.get('name','(untitled)')} "
                         f"(ID {m.get('id')}, {m.get('state','')})")
            if params.include_items:
                for it in m.get("items", []) or []:
                    title = it.get("title", "")
                    itype = it.get("type", "")
                    url = it.get("html_url", "")
                    ref = ""
                    if itype == "Page" and it.get("page_url"):
                        ref = f" (page: {it['page_url']})"
                    elif it.get("content_id") is not None:
                        ref = f" (id: {it['content_id']})"
                    lines.append(f"- [{itype}] {title}{ref}{f' — {url}' if url else ''}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_get_syllabus",
    annotations={"title": "Get Course Syllabus", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_get_syllabus(params: SyllabusInput) -> str:
    """Get a course's syllabus body as plain text.

    Args:
        params.course_id: Canvas course ID.

    Returns:
        str: The course name and its syllabus content (HTML stripped to text).
    """
    try:
        c = await _get(f"/courses/{params.course_id}",
                       {"include[]": ["syllabus_body"]})
        body = _strip_html(c.get("syllabus_body"), limit=8000)
        if not body:
            msg = (f"No syllabus content is published for "
                   f"**{_course_name(c)}** (ID {params.course_id}).")
            try:
                matches = await _paginated_get(
                    f"/courses/{params.course_id}/files",
                    {"search_term": "syllabus"}, max_items=10)
            except Exception:  # noqa: BLE001
                matches = []
            matches = [m for m in matches if isinstance(m, dict)]
            if matches:
                return (msg + f"\n\nHowever, {len(matches)} file(s) matching "
                        f"'syllabus' were found in Course Files — try "
                        f"canvas_get_file on one of these:\n"
                        + "\n".join(
                            f"- **{f.get('display_name') or f.get('filename')}** "
                            f"(file ID {f.get('id')})" for f in matches))
            return (msg + " Check canvas_get_home_page or canvas_list_files for "
                    "this course, or look for the syllabus mentioned in "
                    "canvas_list_announcements.")
        return f"# Syllabus — {_course_name(c)}\n\n{body}"
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


# --------------------------------------------------------------------------- #
# Write infrastructure (used only by the confirm-gated write tools below)
# --------------------------------------------------------------------------- #

async def _send(method: str, path: str,
                data: Optional[dict[str, Any]] = None) -> Any:
    """Form-encoded write request (POST/PUT) returning parsed JSON.

    Canvas write endpoints expect application/x-www-form-urlencoded bodies with
    bracketed nested keys (e.g. ``submission[submission_type]``). Pass those keys
    directly in ``data``; list values are encoded as repeated keys by httpx.
    """
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    await _dns_ready()
    resp = await _api_client().request(method, url, headers=_headers(), data=data)
    resp.raise_for_status()
    if resp.text.strip():
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}
    return {}


async def _fetch_text(url: str, limit: int = HTML_TEXT_LIMIT) -> Optional[str]:
    """Download a (signed) Canvas file URL and return decoded text.

    Returns None if the content is not text-like (binary: pdf, docx, images …).
    No auth header is sent: Canvas file URLs are pre-signed and live on a
    different host.
    """
    async with _canvas_client(timeout=REQUEST_TIMEOUT,
                              follow_redirects=True,
                              headers={"Accept": "*/*"}) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "").lower()
        if not any(t in ctype for t in
                   ("text/", "json", "csv", "xml", "javascript", "markdown")):
            return None
        text = resp.text
        if len(text) > limit:
            text = text[:limit].rstrip() + "\n…[truncated]"
        return text


async def _download_bytes(url: str) -> bytes:
    """Download a (signed) Canvas file URL. No auth header: the URL is pre-signed."""
    async with _canvas_client(timeout=REQUEST_TIMEOUT, follow_redirects=True,
                              headers={"Accept": "*/*"}) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


def _extract_pdf_text(content: bytes, limit: int = HTML_TEXT_LIMIT,
                      max_pages: int = 40) -> str:
    """Best-effort text extraction from PDF bytes. Returns '' if the PDF parses
    successfully but has no extractable text layer (e.g. a scanned image PDF).
    Raises on a genuinely unparseable/corrupt PDF or a missing pypdf install —
    callers should catch that separately from an empty-string ("no text layer")
    result so the user gets an accurate explanation.
    """
    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content))
    pages_text = []
    for i, page in enumerate(reader.pages):
        if i >= max_pages:
            pages_text.append(
                f"\n…[{len(reader.pages) - max_pages} more page(s) not extracted]")
            break
        # Plain mode puts each word on its own line for some PDFs (Ateneo
        # syllabi); layout mode keeps lines and tables, padded with spaces.
        try:
            pages_text.append(page.extract_text(extraction_mode="layout") or "")
        except Exception:  # noqa: BLE001
            try:
                pages_text.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001
                continue
    text = "\n\n".join(t for t in pages_text if t.strip())
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n…[truncated]"
    return text


def _dry_run(action: str, *, target: str = "", payload: str = "") -> str:
    """Standard preview returned by a write tool when confirm is False."""
    lines = ["⚠️ DRY RUN — nothing was sent to Canvas.", "",
             f"This call WOULD: {action}"]
    if target:
        lines.append(f"  • Target: {target}")
    if payload:
        lines.append(f"  • Content: {payload}")
    lines += ["", "Nothing has changed yet. To actually do this, call the same "
              "tool again with `confirm=true`."]
    return "\n".join(lines)


def _excerpt(text: Optional[str], n: int = 300) -> str:
    text = (text or "").strip()
    if not text:
        return "(empty)"
    return f"{text[:n]}{'…' if len(text) > n else ''} [{len(text)} chars]"


def _render_entries(entries: Optional[list[dict[str, Any]]],
                    participants: dict[Any, str], depth: int = 0) -> list[str]:
    """Recursively render a Canvas discussion 'view' entry tree to markdown.

    Recurses into a deleted entry's replies even though the deleted entry
    itself is not rendered — a live reply nested under a deleted parent
    must not become invisible.
    """
    out: list[str] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        if not e.get("deleted"):
            author = participants.get(e.get("user_id"), "Unknown")
            when = _fmt_dt(e.get("created_at"))
            msg = _strip_html(e.get("message"), limit=1500) or "(no text)"
            indent = "    " * depth
            out.append(f"{indent}- **{author}** · {when}\n{indent}  {msg}")
        out.extend(_render_entries(e.get("replies"), participants, depth + 1))
    return out


def _collect_own_entries(entries: Optional[list[dict[str, Any]]], self_id: Any,
                         out: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    """Recursively collect every entry (at any depth) authored by self_id.

    Recurses into a deleted entry's replies even though the deleted entry
    itself is not collected — a live reply nested under a deleted parent
    must not become invisible.
    """
    if out is None:
        out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        if not e.get("deleted") and e.get("user_id") == self_id:
            out.append(e)
        _collect_own_entries(e.get("replies"), self_id, out)
    return out


# --------------------------------------------------------------------------- #
# Input models — new read tools
# --------------------------------------------------------------------------- #

class ListPagesInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    search_term: Optional[str] = Field(
        default=None, description="Optional case-insensitive title filter.")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class GetPageInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    page: str = Field(
        ...,
        description="Page URL slug (e.g. 'course-syllabus') or numeric page ID. "
                    "Get these from canvas_list_pages, or from the '(page: …)' "
                    "hint in canvas_list_modules.")


class ListFilesInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    search_term: Optional[str] = Field(
        default=None, description="Optional case-insensitive filename filter.")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class GetFileInput(_Base):
    file_id: int = Field(..., description="Canvas file ID (from canvas_list_files).",
                         ge=1)
    include_content: bool = Field(
        default=True,
        description="For text-based files (txt, md, csv, html, json), download and "
                    "inline the file's text. PDFs are downloaded and their text "
                    "layer extracted (best effort — layout and images are lost, "
                    "large files are skipped). Other binary files (docx, images) "
                    "return metadata and a download URL only.")


DOWNLOAD_DIR = Path(os.environ.get("CANVAS_DOWNLOAD_DIR", "~/Downloads/Canvas")).expanduser()

# Local files the tools may read (uploads, attachments) or write (downloads).
# Anything else — system folders, SSH keys, this server's own .env token — is
# refused, so a tricked model or an exposed HTTP endpoint can't touch it.
FILE_ROOTS = [Path(r).expanduser().resolve() for r in
              os.environ.get("CANVAS_FILE_ROOTS", "~/Downloads:~/Desktop:~/Documents")
              .split(os.pathsep) if r.strip()]
_SERVER_DIR = Path(__file__).resolve().parent


def _safe_path(raw: Any) -> Path:
    """Resolve a local path (following symlinks) and refuse it unless it sits
    inside FILE_ROOTS, outside this server's folder, with no hidden component."""
    p = Path(str(raw)).expanduser().resolve()
    root = next((r for r in FILE_ROOTS if p.is_relative_to(r)), None)
    if root is None:
        raise CanvasError(f"{p} is outside the folders this server may use: "
                          + ", ".join(map(str, FILE_ROOTS)))
    if p.is_relative_to(_SERVER_DIR) or any(
            part.startswith(".") for part in p.relative_to(root).parts):
        raise CanvasError(f"{p} is a hidden or protected path; refusing to use it.")
    return p


class DownloadFileInput(_Base):
    file_id: int = Field(..., description="Canvas file ID (from canvas_list_files, "
                                          "modules, or an assignment).", ge=1)
    dest_dir: Optional[str] = Field(
        default=None,
        description=f"Folder to save into (inside Downloads, Desktop, or Documents). "
                    f"Defaults to {DOWNLOAD_DIR}. Created if missing.")


class ListDiscussionsInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class GetDiscussionInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    topic_id: int = Field(...,
                          description="Discussion topic ID (from canvas_list_discussions).",
                          ge=1)
    include_replies: bool = Field(
        default=True, description="Include the full threaded reply tree.")
    only_mine: bool = Field(
        default=False,
        description="If True, skip the full reply tree and instead show only "
                    "entries authored by you — useful for checking whether "
                    "you've already posted to this discussion. Implies "
                    "include_replies (it still needs the full thread to search).")


class ListQuizzesInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class GetQuizInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    quiz_id: int = Field(..., description="Quiz ID (from canvas_list_quizzes).", ge=1)


class CalendarInput(_Base):
    start_date: Optional[str] = Field(
        default=None, description="ISO date YYYY-MM-DD. Defaults to today.")
    end_date: Optional[str] = Field(
        default=None, description="ISO date YYYY-MM-DD. Defaults to 14 days after start.")
    event_type: str = Field(
        default="event", pattern=r"^(event|assignment)$",
        description="'event' for calendar events (class meetings, etc.) or "
                    "'assignment' for assignment due dates.")
    course_id: Optional[CourseRef] = Field(
        default=None,
        description="Limit to one course. Omit for all active courses plus your "
                    "personal calendar.")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class TodoInput(_Base):
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class ListTabsInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class GetHomePageInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)


class ListPeopleInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    enrollment_type: Optional[str] = Field(
        default=None,
        description="Filter by role: 'student', 'teacher', 'ta', 'observer', or "
                    "'designer'. Omit to list all members.",
        pattern=r"^(student|teacher|ta|observer|designer)$",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class ListConferencesInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class ListCollaborationsInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class ListOutcomesInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


# --------------------------------------------------------------------------- #
# Input models — write tools (each carries a `confirm` safety gate)
# --------------------------------------------------------------------------- #

_CONFIRM = Field(
    default=False,
    description="Safety gate. Leave False (default) to get a preview of what "
                "would happen without sending anything to Canvas. Set True only "
                "when you actually want to perform the write.")


class CreatePlannerNoteInput(_Base):
    title: str = Field(..., min_length=1, description="Title of the planner note / to-do.")
    todo_date: str = Field(
        ..., description="When it should appear, ISO-8601 (YYYY-MM-DD or full "
                         "timestamp like 2026-06-27T09:00:00Z).")
    details: Optional[str] = Field(default=None, description="Optional longer description.")
    course_id: Optional[CourseRef] = Field(
        default=None, description="Optional course to associate the note with.")
    confirm: bool = _CONFIRM


class SubmitAssignmentInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    assignment_id: int = Field(..., description="Canvas assignment ID.", ge=1)
    submission_type: str = Field(
        ..., pattern=r"^(online_text_entry|online_url|online_upload)$",
        description="One of 'online_text_entry', 'online_url', or 'online_upload'. "
                    "Must be allowed by the assignment.")
    body: Optional[str] = Field(
        default=None, description="Text/HTML content. Required for online_text_entry.")
    url: Optional[str] = Field(
        default=None, description="URL to submit. Required for online_url.")
    file_ids: Optional[list[int]] = Field(
        default=None,
        description="Canvas file IDs already uploaded for this submission. For "
                    "online_upload, prefer local_paths.")
    local_paths: Optional[list[str]] = Field(
        default=None,
        description="For online_upload: absolute paths of files in Downloads, Desktop, "
                    "or Documents. Uploaded to the assignment and submitted on confirm.")
    confirm: bool = _CONFIRM


class PostDiscussionEntryInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    topic_id: int = Field(..., description="Discussion topic ID.", ge=1)
    message: str = Field(..., min_length=1, description="Body of the new top-level entry.")
    confirm: bool = _CONFIRM


class ReplyToEntryInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    topic_id: int = Field(..., description="Discussion topic ID.", ge=1)
    entry_id: int = Field(..., description="ID of the entry being replied to.", ge=1)
    message: str = Field(..., min_length=1, description="Body of the reply.")
    confirm: bool = _CONFIRM


class PostSubmissionCommentInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    assignment_id: int = Field(..., description="Canvas assignment ID.", ge=1)
    comment: str = Field(..., min_length=1,
                         description="Comment text to attach to your submission.")
    confirm: bool = _CONFIRM


class UploadFileInput(_Base):
    course_id: CourseRef = Field(..., description="Canvas course ID to upload into.")
    local_path: str = Field(
        ..., min_length=1,
        description="Absolute path to the file (in Downloads, Desktop, or Documents).")
    display_name: Optional[str] = Field(
        default=None,
        description="Name to show in Canvas. Defaults to the file's own filename.")
    folder_path: Optional[str] = Field(
        default=None,
        description="Destination folder path inside the course files (e.g. "
                    "'unfiled' or 'Submissions'). Defaults to course root.")
    confirm: bool = _CONFIRM


class GetSubmissionInput(_Base):
    course_id: CourseRef = Field(..., description=COURSE_DESC)
    assignment_id: int = Field(..., description="Canvas assignment ID.", ge=1)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN,
                                            description="'markdown' or 'json'.")


class SendMessageInput(_Base):
    recipient_ids: list[str] = Field(
        ..., min_length=1,
        description="List of Canvas user IDs or login IDs to send to. "
                    "Use canvas_get_profile to find your own ID; instructor IDs "
                    "appear in announcement/discussion author data.")
    subject: str = Field(..., min_length=1, description="Message subject line.")
    body: str = Field(..., min_length=1, description="Message body text.")
    course_id: Optional[CourseRef] = Field(
        default=None,
        description="Optional course ID. When given, sets the conversation context "
                    "so the message is associated with that course.")
    local_paths: Optional[list[str]] = Field(
        default=None,
        description="Absolute paths of files to attach (in Downloads, Desktop, or "
                    "Documents). Uploaded to your Canvas files, then attached.")
    attachment_ids: Optional[list[int]] = Field(
        default=None,
        description="Canvas file IDs of already-uploaded files to attach. "
                    "Use canvas_upload_file to get a file_id first.")
    confirm: bool = _CONFIRM


# --------------------------------------------------------------------------- #
# Read tools — pages
# --------------------------------------------------------------------------- #

@_tool(
    name="canvas_list_pages",
    annotations={"title": "List Course Pages", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_pages(params: ListPagesInput) -> str:
    """List the wiki Pages in a course (title, URL slug, page ID, published state).

    Many instructors put the real syllabus, schedules, and instructions in a Page
    rather than the official syllabus field — use this to find them, then
    canvas_get_page to read one.

    Args:
        params.course_id: Canvas course ID.
        params.search_term: Optional title filter.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Each page — title, slug (for canvas_get_page), page ID, updated date.
    """
    try:
        api_params: dict[str, Any] = {"sort": "title"}
        if params.search_term:
            api_params["search_term"] = params.search_term
        pages = await _paginated_get(f"/courses/{params.course_id}/pages", api_params)
        slim = [{
            "title": p.get("title"), "slug": p.get("url"),
            "page_id": p.get("page_id"), "published": p.get("published"),
            "updated_at": p.get("updated_at"),
        } for p in pages if isinstance(p, dict)]

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"course_id": params.course_id, "count": len(slim),
                               "pages": slim}, indent=2)
        if not slim:
            return f"No pages found for course {params.course_id}."
        lines = [f"**{len(slim)} page(s) in course {params.course_id}:**\n"]
        for p in slim:
            pub = "" if p["published"] else " _(unpublished)_"
            lines.append(f"- **{p['title']}**{pub} — slug: `{p['slug']}` "
                         f"· updated {_fmt_dt(p['updated_at'])}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_get_page",
    annotations={"title": "Get Page Content", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_get_page(params: GetPageInput) -> str:
    """Read the full body of a single Canvas Page as plain text.

    Args:
        params.course_id: Canvas course ID.
        params.page: Page URL slug or numeric page ID.

    Returns:
        str: The page title and its body content (HTML stripped to text).
    """
    try:
        p = await _get(f"/courses/{params.course_id}/pages/{params.page}")
        body = _strip_html(p.get("body"), limit=8000)
        header = (f"# {p.get('title','(untitled page)')}\n"
                  f"_Updated {_fmt_dt(p.get('updated_at'))}"
                  f"{' · ' + p['html_url'] if p.get('html_url') else ''}_\n")
        if not body:
            return header + "\n(This page has no text content.)"
        return f"{header}\n{body}"
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


# --------------------------------------------------------------------------- #
# Read tools — files
# --------------------------------------------------------------------------- #

@_tool(
    name="canvas_list_files",
    annotations={"title": "List Course Files", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_files(params: ListFilesInput) -> str:
    """List files uploaded to a course (name, file ID, type, size, download URL).

    Args:
        params.course_id: Canvas course ID.
        params.search_term: Optional filename filter.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Each file — display name, ID (for canvas_get_file), content type, size.
    """
    try:
        api_params: dict[str, Any] = {"sort": "name"}
        if params.search_term:
            api_params["search_term"] = params.search_term
        files = await _paginated_get(f"/courses/{params.course_id}/files", api_params)
        slim = [{
            "id": f.get("id"), "name": f.get("display_name") or f.get("filename"),
            "content_type": f.get("content-type") or f.get("content_type"),
            "size": f.get("size"), "url": f.get("url"),
            "updated_at": f.get("updated_at"),
        } for f in files if isinstance(f, dict)]

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"course_id": params.course_id, "count": len(slim),
                               "files": slim}, indent=2)
        if not slim:
            return f"No files found for course {params.course_id}."
        lines = [f"**{len(slim)} file(s) in course {params.course_id}:**\n"]
        for f in slim:
            size = f["size"]
            size_str = f"{size/1024:.0f} KB" if isinstance(size, int) else "?"
            lines.append(f"- **{f['name']}** (ID {f['id']}) — "
                         f"{f['content_type'] or '?'}, {size_str}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_get_file",
    annotations={"title": "Get File", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_get_file(params: GetFileInput) -> str:
    """Get a file's metadata and, for text-based files and PDFs, its content.

    Text files (txt, md, csv, html, json) are downloaded and inlined. PDFs are
    downloaded and their text layer extracted (best effort: layout, tables, and
    images are lost; only the first 40 pages are read; files over 15 MB and
    scanned/image-only PDFs are not extracted). Other binary files (docx,
    images) return metadata plus a download URL the user can open.

    Args:
        params.file_id: Canvas file ID.
        params.include_content: Inline text content for text-based files and PDFs.

    Returns:
        str: File metadata, plus inlined text content when applicable.
    """
    try:
        f = await _get(f"/files/{params.file_id}")
        ctype = f.get("content-type") or f.get("content_type") or "?"
        name = f.get("display_name") or f.get("filename") or "(file)"
        url = f.get("url")
        size = f.get("size")
        size_str = f"{size/1024:.0f} KB" if isinstance(size, int) else "?"
        out = [f"# {name}",
               f"- File ID: {f.get('id')}",
               f"- Type: {ctype}",
               f"- Size: {size_str}",
               f"- Updated: {_fmt_dt(f.get('updated_at'))}",
               f"- Download: {url or '—'}"]
        is_pdf = "pdf" in str(ctype).lower() or name.lower().endswith(".pdf")
        if params.include_content and url and is_pdf:
            if isinstance(size, int) and size > 15 * 1024 * 1024:
                out.append(f"\n_(PDF is {size / (1024 * 1024):.1f} MB — too large "
                           f"to extract automatically. Use the download URL above.)_")
            else:
                try:
                    content = await _download_bytes(url)
                except Exception:  # noqa: BLE001
                    content = b""
                extraction_failed = False
                text = ""
                if content:
                    try:
                        text = _extract_pdf_text(content, limit=8000)
                    except Exception:  # noqa: BLE001
                        extraction_failed = True
                if text:
                    out.append("\n## Content (extracted from PDF)\n" + text)
                    out.append("\n_(Text extracted from PDF — layout, tables, and "
                               "images are lost. If this looks garbled or "
                               "incomplete, use the download URL above.)_")
                elif not content:
                    out.append("\n_(Could not download the PDF to extract its text. "
                               "Use the download URL above to open it.)_")
                elif extraction_failed:
                    out.append("\n_(This PDF could not be parsed — it may be "
                               "corrupted, encrypted, or password-protected. Use "
                               "the download URL above to open it.)_")
                else:
                    out.append("\n_(PDF text extraction was attempted but found no "
                               "extractable text — this is likely a scanned or "
                               "image-only PDF. Use the download URL above to open "
                               "it.)_")
        elif params.include_content and url:
            try:
                text = await _fetch_text(url, limit=8000)
            except Exception:  # noqa: BLE001
                text = None
            if text is not None:
                out.append("\n## Content\n" + text)
            else:
                out.append("\n_(Binary file — not inlined. Use the download URL "
                           "above to open it.)_")
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


def _free_path(folder: Path, name: str) -> Path:
    """folder/name, or 'name (2).ext' etc. — never overwrites an existing file."""
    name = Path(name.replace("\\", "/")).name or "download"
    path = folder / name
    n = 2
    while path.exists():
        path = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    return path


@_tool(
    name="canvas_download_file",
    annotations={"title": "Download File to This Computer", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def canvas_download_file(params: DownloadFileInput) -> str:
    """Save a Canvas file (syllabus PDF, slides, worksheet, …) to this computer.

    Saves into ~/Downloads/Canvas by default (or dest_dir). Never overwrites: a
    name clash gets a " (2)" suffix. Changes nothing on Canvas. To read a file's
    text instead of saving it, use canvas_get_file.

    Args:
        params.file_id: Canvas file ID.
        params.dest_dir: Optional destination folder.

    Returns:
        str: The saved file's full path and size.
    """
    try:
        f = await _get(f"/files/{params.file_id}")
        url = f.get("url")
        if not url:
            return ("Error: Canvas gave no download link for this file — it may be "
                    "locked or not yet published to students.")
        folder = _safe_path(params.dest_dir or DOWNLOAD_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        dest = _free_path(folder, f.get("display_name") or f.get("filename") or "download")
        part = dest.with_name(dest.name + ".part")
        try:
            async with _canvas_client(timeout=120.0, follow_redirects=True,
                                      headers={"Accept": "*/*"}) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with part.open("wb") as out:
                        async for chunk in resp.aiter_bytes():
                            out.write(chunk)
            part.rename(dest)
        finally:
            part.unlink(missing_ok=True)
        return f"✅ Saved {dest} ({dest.stat().st_size / 1024:.0f} KB)"
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


# --------------------------------------------------------------------------- #
# Read tools — discussions
# --------------------------------------------------------------------------- #

@_tool(
    name="canvas_list_discussions",
    annotations={"title": "List Discussions", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_discussions(params: ListDiscussionsInput) -> str:
    """List discussion topics in a course (excludes announcements).

    Args:
        params.course_id: Canvas course ID.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Each topic — title, ID (for canvas_get_discussion), reply count, posted date.
    """
    try:
        topics = await _paginated_get(
            f"/courses/{params.course_id}/discussion_topics", {})
        slim = [{
            "id": t.get("id"), "title": t.get("title"),
            "posted_at": t.get("posted_at") or t.get("created_at"),
            "replies": t.get("discussion_subentry_count"),
            "url": t.get("html_url"),
        } for t in topics if isinstance(t, dict)]
        slim.sort(key=lambda r: r["posted_at"] or "", reverse=True)

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"course_id": params.course_id, "count": len(slim),
                               "discussions": slim}, indent=2)
        if not slim:
            return f"No discussion topics found for course {params.course_id}."
        lines = [f"**{len(slim)} discussion(s) in course {params.course_id}:**\n"]
        for t in slim:
            rc = t["replies"]
            lines.append(f"- **{t['title']}** (ID {t['id']}) — "
                         f"{rc if rc is not None else 0} repl"
                         f"{'y' if rc == 1 else 'ies'} · {_fmt_dt(t['posted_at'])}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_get_discussion",
    annotations={"title": "Get Discussion Thread", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_get_discussion(params: GetDiscussionInput) -> str:
    """Read a discussion topic's prompt and (optionally) its full threaded replies.

    Args:
        params.course_id: Canvas course ID.
        params.topic_id: Discussion topic ID.
        params.include_replies: Include the threaded reply tree.
        params.only_mine: Show only your own entries instead of the full tree —
            use this to check whether you have already posted.

    Returns:
        str: The topic prompt, then each reply with author, timestamp, and text.
    """
    try:
        t = await _get(
            f"/courses/{params.course_id}/discussion_topics/{params.topic_id}")
        out = [f"# {t.get('title','(untitled discussion)')}",
               f"_Posted {_fmt_dt(t.get('posted_at') or t.get('created_at'))}"
               f"{' · ' + t['html_url'] if t.get('html_url') else ''}_\n",
               "## Prompt", _strip_html(t.get("message"), limit=4000) or "(no prompt text)"]

        if params.include_replies or params.only_mine:
            try:
                view = await _get(
                    f"/courses/{params.course_id}/discussion_topics/"
                    f"{params.topic_id}/view")
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403:
                    if params.only_mine:
                        out.append(
                            "\n## Your entries\n_Cannot determine whether you've "
                            "posted: the replies endpoint returned 403 Forbidden "
                            "from Canvas (this can happen even when the topic "
                            "prompt itself is readable). Check the discussion in "
                            "the Canvas web UI, or retry later._")
                    else:
                        out.append(
                            "\n## Replies\n_Could not load replies (403 Forbidden "
                            "from Canvas on the replies endpoint — this can happen "
                            "even when the topic prompt itself is readable). Call "
                            "this tool again with include_replies=false to skip "
                            "retrying it, or retry later._")
                    return "\n".join(out)
                raise
            if params.only_mine:
                me = await _get("/users/self")
                mine = _collect_own_entries(view.get("view", []), me.get("id"))
                out.append(f"\n## Your entries ({len(mine)})")
                if not mine:
                    out.append("(no entries found — you have not posted to this "
                               "discussion yet)")
                else:
                    for entry in mine:
                        when = _fmt_dt(entry.get("created_at"))
                        msg = _strip_html(entry.get("message"),
                                          limit=1500) or "(no text)"
                        out.append(f"- **{when}**\n  {msg}")
            else:
                participants = {p.get("id"): p.get("display_name", "Unknown")
                                for p in view.get("participants", [])
                                if isinstance(p, dict)}
                entries = view.get("view", [])
                rendered = _render_entries(entries, participants)
                out.append(f"\n## Replies ({len(rendered) and 'threaded' or 'none'})")
                out.extend(rendered or ["(no replies yet)"])
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


# --------------------------------------------------------------------------- #
# Read tools — quizzes
# --------------------------------------------------------------------------- #

@_tool(
    name="canvas_list_quizzes",
    annotations={"title": "List Quizzes", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_quizzes(params: ListQuizzesInput) -> str:
    """List quizzes in a course with due dates, points, and question counts.

    Args:
        params.course_id: Canvas course ID.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Each quiz — title, ID, due date, points, question count, type.
    """
    try:
        quizzes = await _paginated_get(f"/courses/{params.course_id}/quizzes", {})
        slim = [{
            "id": q.get("id"), "title": q.get("title"),
            "due_at": q.get("due_at"), "points_possible": q.get("points_possible"),
            "question_count": q.get("question_count"),
            "quiz_type": q.get("quiz_type"), "time_limit": q.get("time_limit"),
            "allowed_attempts": q.get("allowed_attempts"),
            "html_url": q.get("html_url"),
        } for q in quizzes if isinstance(q, dict)]
        slim.sort(key=lambda r: r["due_at"] or "9999")

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"course_id": params.course_id, "count": len(slim),
                               "quizzes": slim}, indent=2)
        if not slim:
            return f"No quizzes found for course {params.course_id}."
        lines = [f"**{len(slim)} quiz(zes) in course {params.course_id}:**\n"]
        for q in slim:
            pts = q["points_possible"]
            lines.append(f"- **{q['title']}** (ID {q['id']}) — due "
                         f"{_fmt_dt(q['due_at'])}"
                         f"{f', {pts} pts' if pts is not None else ''}, "
                         f"{q['question_count'] or 0} Qs [{q['quiz_type'] or '?'}]")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_get_quiz",
    annotations={"title": "Get Quiz Detail", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_get_quiz(params: GetQuizInput) -> str:
    """Get a quiz's full settings and instructions.

    Note: individual quiz questions are generally not retrievable via the API
    before/outside an attempt, so this returns settings and the description only.

    Args:
        params.course_id: Canvas course ID.
        params.quiz_id: Quiz ID.

    Returns:
        str: Title, timing, attempts, points, and the quiz description/instructions.
    """
    try:
        q = await _get(f"/courses/{params.course_id}/quizzes/{params.quiz_id}")
        tl = q.get("time_limit")
        out = [f"# {q.get('title','(untitled quiz)')}",
               f"- Quiz ID: {q.get('id')} · Type: {q.get('quiz_type','?')}",
               f"- Due: {_fmt_dt(q.get('due_at'))}",
               f"- Available: {_fmt_dt(q.get('unlock_at'))} → {_fmt_dt(q.get('lock_at'))}",
               f"- Points possible: {q.get('points_possible','—')}",
               f"- Questions: {q.get('question_count','—')}",
               f"- Time limit: {f'{tl} min' if tl else 'none'}",
               f"- Allowed attempts: {q.get('allowed_attempts','—')}",
               f"- Link: {q.get('html_url','—')}"]
        desc = _strip_html(q.get("description"))
        if desc:
            out.append("\n## Instructions\n" + desc)
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


# --------------------------------------------------------------------------- #
# Read tools — calendar & to-do
# --------------------------------------------------------------------------- #

@_tool(
    name="canvas_list_calendar_events",
    annotations={"title": "List Calendar Events", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_calendar_events(params: CalendarInput) -> str:
    """List calendar events or assignment due-dates in a date range.

    Use event_type='event' for class meetings and scheduled events; use
    event_type='assignment' for assignment due dates shown on the calendar.

    Args:
        params.start_date / params.end_date: ISO dates (defaults: today → +14 days).
        params.event_type: 'event' or 'assignment'.
        params.course_id: Limit to one course, or omit for all active courses + personal.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Each event — title, start/end, location, course context.
    """
    try:
        start = params.start_date or _now().date().isoformat()
        end = params.end_date or (_now() + timedelta(days=14)).date().isoformat()

        if params.course_id:
            contexts = [f"course_{params.course_id}"]
        else:
            courses = await _fetch_courses("active")
            contexts = [f"course_{c['id']}" for c in courses]
            try:
                me = await _get("/users/self")
                if me.get("id"):
                    contexts.append(f"user_{me['id']}")
            except Exception:  # noqa: BLE001
                pass
            contexts = contexts[:10]  # Canvas caps context_codes at 10 per request.

        events = await _paginated_get("/calendar_events", {
            "type": params.event_type,
            "start_date": start, "end_date": end,
            "context_codes[]": contexts,
        }, max_items=200)

        rows = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            rows.append({
                "title": ev.get("title"),
                "start_at": ev.get("start_at"),
                "end_at": ev.get("end_at"),
                "location": ev.get("location_name"),
                "context": ev.get("context_name") or ev.get("context_code"),
                "url": ev.get("html_url"),
            })
        rows.sort(key=lambda r: r["start_at"] or "9999")

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"type": params.event_type, "start": start, "end": end,
                               "count": len(rows), "events": rows}, indent=2)
        if not rows:
            return (f"No {params.event_type}s on the calendar between {start} "
                    f"and {end}.")
        label = "assignment due date" if params.event_type == "assignment" else "event"
        lines = [f"**{len(rows)} {label}(s) — {start} to {end}:**\n"]
        for r in rows:
            loc = f" @ {r['location']}" if r["location"] else ""
            ctx = f" · {r['context']}" if r["context"] else ""
            lines.append(f"- **{_fmt_dt(r['start_at'])}** — {r['title']}{loc}{ctx}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_list_todo",
    annotations={"title": "List To-Do", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_todo(params: TodoInput) -> str:
    """List Canvas's own to-do items for the user (things needing action).

    This is Canvas's native to-do list — typically upcoming assignments/quizzes
    that have not yet been submitted, across all courses. Still-actionable graded
    discussion topics are also swept in, because Canvas's own to-do endpoint does
    not reliably surface them.

    Args:
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Each item — title, course, due date, type, link.
    """
    try:
        items = await _paginated_get("/users/self/todo", {}, max_items=100)
        rows = []
        for it in items:
            if not isinstance(it, dict):
                continue
            obj = it.get("assignment") or it.get("quiz") or {}
            rows.append({
                "id": obj.get("id"),
                "type": it.get("type"),
                "title": obj.get("name") or obj.get("title") or "(untitled)",
                "course": it.get("context_name"),
                "due_at": obj.get("due_at"),
                "points": obj.get("points_possible"),
                "url": it.get("html_url") or obj.get("html_url"),
            })

        # Canvas's to-do endpoint omits graded discussions — add the actionable ones.
        # Prefer matching on the Canvas assignment ID (stable); fall back to
        # course-name + title only when either side has no ID.
        def _already_listed(d: dict[str, Any]) -> bool:
            d_id = d.get("id")
            for r in rows:
                r_id = r.get("id")
                if d_id is not None and r_id is not None:
                    if d_id == r_id:
                        return True
                elif (r["course"], r["title"]) == (d["context"], d["title"]):
                    return True
            return False

        discussions, disc_skipped = await _fetch_graded_discussions()
        for d in discussions:
            if d["status"] not in ("missing", "not submitted"):
                continue
            if _already_listed(d):
                continue
            rows.append({
                "id": d.get("id"),
                "type": d["type"],
                "title": d["title"],
                "course": d["context"],
                "due_at": d["due"],
                "points": d["points"],
                "url": d["html_url"],
            })
        rows.sort(key=lambda r: r["due_at"] or "9999")

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"count": len(rows), "todo": rows,
                               "skipped_courses": disc_skipped}, indent=2)
        note = ""
        if disc_skipped:
            note = (f"_(Note: could not check {', '.join(disc_skipped)} — you may "
                    f"be missing items from "
                    f"{'this course' if len(disc_skipped) == 1 else 'these courses'}"
                    f".)_")
        if not rows:
            empty = "Your Canvas to-do list is empty."
            return f"{note}\n\n{empty}" if note else empty
        lines = []
        if note:
            lines.append(note + "\n")
        lines.append(f"**{len(rows)} Canvas to-do item(s):**\n")
        for r in rows:
            pts = f", {r['points']} pts" if r["points"] is not None else ""
            ctx = f" · {r['course']}" if r["course"] else ""
            lines.append(f"- **{r['title']}** — due {_fmt_dt(r['due_at'])}"
                         f"{pts}{ctx} [{r['type']}]")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


# --------------------------------------------------------------------------- #
# Write tools — each refuses to act unless confirm=True
# --------------------------------------------------------------------------- #

@_tool(
    name="canvas_create_planner_note",
    annotations={"title": "Create Planner Note", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def canvas_create_planner_note(params: CreatePlannerNoteInput) -> str:
    """Create a personal planner note / to-do in Canvas (private to you).

    Low-stakes and reversible. Still gated by `confirm` for consistency: the first
    call previews; set confirm=True to actually create it.

    Args:
        params.title / params.todo_date: Required note title and date.
        params.details: Optional longer text.
        params.course_id: Optional course association.
        params.confirm: Must be True to create.

    Returns:
        str: Dry-run preview, or confirmation with the new note's ID and date.
    """
    try:
        if not params.confirm:
            tgt = f"course {params.course_id}" if params.course_id else "personal calendar"
            return _dry_run(f"create a planner note dated {params.todo_date}",
                            target=tgt,
                            payload=f"{params.title!r} — {_excerpt(params.details, 200)}")
        data: dict[str, Any] = {"title": params.title, "todo_date": params.todo_date}
        if params.details:
            data["details"] = params.details
        if params.course_id:
            data["course_id"] = params.course_id
        note = await _send("POST", "/planner_notes", data)
        return (f"✅ Planner note created (ID {note.get('id','?')}) for "
                f"{_fmt_dt(note.get('todo_date') or params.todo_date)}: "
                f"{note.get('title', params.title)}")
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_submit_assignment",
    annotations={"title": "Submit Assignment", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
)
async def canvas_submit_assignment(params: SubmitAssignmentInput) -> str:
    """Submit work to an assignment. WRITE — gated by confirm.

    Supports text entries, URL submissions, and file uploads (local_paths: files
    on this computer, uploaded to the assignment on confirm). The first call returns a preview including
    the assignment name, due date, and your current submission state. Set
    confirm=True to actually submit.

    Args:
        params.course_id / params.assignment_id: Target assignment.
        params.submission_type: 'online_text_entry', 'online_url', or 'online_upload'.
        params.body / params.url / params.local_paths (or file_ids): The matching payload.
        params.confirm: Must be True to submit.

    Returns:
        str: Dry-run preview, or confirmation with the new submission state.
    """
    try:
        # Validate the payload matches the chosen submission type.
        need = {"online_text_entry": "body", "online_url": "url",
                "online_upload": "file_ids"}[params.submission_type]
        if getattr(params, need) in (None, "", []) and not (
                need == "file_ids" and params.local_paths):
            need = "local_paths" if need == "file_ids" else need
            return (f"Error: submission_type '{params.submission_type}' requires "
                    f"the '{need}' field, which is missing.")
        local_files = [_safe_path(lp) for lp in params.local_paths or []]
        for lf in local_files:
            if not lf.is_file():
                return f"Error: Local file not found: {lf}"

        if params.submission_type == "online_text_entry":
            payload = f"text: {_excerpt(params.body)}"
        elif params.submission_type == "online_url":
            payload = f"url: {params.url}"
        else:
            payload = ", ".join([f"{lf.name} ({lf.stat().st_size / 1024:.0f} KB)"
                                 for lf in local_files]
                                + [f"file ID {i}" for i in params.file_ids or []])

        if not params.confirm:
            # Enrich the preview with what's actually being submitted to.
            target = f"course {params.course_id}, assignment {params.assignment_id}"
            try:
                a = await _get(f"/courses/{params.course_id}/assignments/"
                               f"{params.assignment_id}", {"include[]": ["submission"]})
                state = _submission_summary(a.get("submission"))
                target = (f"“{a.get('name','?')}” (assignment {params.assignment_id}, "
                          f"course {params.course_id}) — due {_fmt_dt(a.get('due_at'))}; "
                          f"current submission: {state}")
            except Exception:  # noqa: BLE001
                pass
            return _dry_run(f"submit a {params.submission_type} to this assignment",
                            target=target, payload=payload)

        data: dict[str, Any] = {"submission[submission_type]": params.submission_type}
        if params.submission_type == "online_text_entry":
            data["submission[body]"] = params.body
        elif params.submission_type == "online_url":
            data["submission[url]"] = params.url
        else:
            file_ids = list(params.file_ids or [])
            for lf in local_files:
                f = await _upload_file(f"/courses/{params.course_id}/assignments/"
                                       f"{params.assignment_id}/submissions/self/files",
                                       str(lf), None, None)
                file_ids.append(f["id"])
            data["submission[file_ids][]"] = file_ids
        sub = await _send("POST", f"/courses/{params.course_id}/assignments/"
                          f"{params.assignment_id}/submissions", data)
        return (f"✅ Submitted to assignment {params.assignment_id}. "
                f"State: {_submission_summary(sub)}.")
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_post_discussion_entry",
    annotations={"title": "Post Discussion Entry", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
)
async def canvas_post_discussion_entry(params: PostDiscussionEntryInput) -> str:
    """Post a new top-level entry to a discussion topic. WRITE — gated by confirm.

    This is visible to the class. The first call previews the topic title and your
    message; set confirm=True to actually post.

    Args:
        params.course_id / params.topic_id: Target discussion.
        params.message: Body of the entry.
        params.confirm: Must be True to post.

    Returns:
        str: Dry-run preview, or confirmation with the new entry ID.
    """
    try:
        if not params.confirm:
            target = f"topic {params.topic_id} in course {params.course_id}"
            try:
                t = await _get(f"/courses/{params.course_id}/discussion_topics/"
                               f"{params.topic_id}")
                target = f"“{t.get('title','?')}” (topic {params.topic_id})"
            except Exception:  # noqa: BLE001
                pass
            return _dry_run("post a new public discussion entry", target=target,
                            payload=_excerpt(params.message))
        entry = await _send("POST", f"/courses/{params.course_id}/discussion_topics/"
                            f"{params.topic_id}/entries",
                            {"message": params.message})
        return f"✅ Posted entry (ID {entry.get('id','?')}) to topic {params.topic_id}."
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_reply_to_entry",
    annotations={"title": "Reply To Discussion Entry", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
)
async def canvas_reply_to_entry(params: ReplyToEntryInput) -> str:
    """Reply to a specific discussion entry (threaded). WRITE — gated by confirm.

    Args:
        params.course_id / params.topic_id / params.entry_id: Target entry.
        params.message: Body of the reply.
        params.confirm: Must be True to post.

    Returns:
        str: Dry-run preview, or confirmation with the new reply ID.
    """
    try:
        if not params.confirm:
            return _dry_run(
                f"post a public reply to entry {params.entry_id}",
                target=f"topic {params.topic_id} in course {params.course_id}",
                payload=_excerpt(params.message))
        reply = await _send(
            "POST", f"/courses/{params.course_id}/discussion_topics/"
            f"{params.topic_id}/entries/{params.entry_id}/replies",
            {"message": params.message})
        return (f"✅ Replied (ID {reply.get('id','?')}) to entry "
                f"{params.entry_id} in topic {params.topic_id}.")
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_post_submission_comment",
    annotations={"title": "Comment On Submission", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def canvas_post_submission_comment(params: PostSubmissionCommentInput) -> str:
    """Add a comment to your own submission (e.g. a note to the grader).
    WRITE — gated by confirm.

    Args:
        params.course_id / params.assignment_id: Target assignment.
        params.comment: Comment text.
        params.confirm: Must be True to post.

    Returns:
        str: Dry-run preview, or confirmation.
    """
    try:
        if not params.confirm:
            target = f"your submission for assignment {params.assignment_id}"
            try:
                a = await _get(f"/courses/{params.course_id}/assignments/"
                               f"{params.assignment_id}")
                target = f"your submission for “{a.get('name','?')}”"
            except Exception:  # noqa: BLE001
                pass
            return _dry_run("add a comment to your submission", target=target,
                            payload=_excerpt(params.comment))
        await _send("PUT", f"/courses/{params.course_id}/assignments/"
                    f"{params.assignment_id}/submissions/self",
                    {"comment[text_comment]": params.comment})
        return (f"✅ Comment added to your submission for assignment "
                f"{params.assignment_id}.")
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


# --------------------------------------------------------------------------- #
# File upload helper (3-step Canvas upload protocol)
# --------------------------------------------------------------------------- #

async def _upload_file(path: str, local_path: str,
                       display_name: Optional[str],
                       folder_path: Optional[str]) -> dict[str, Any]:
    """Execute Canvas's 3-step file upload and return the resulting file object."""
    p = _safe_path(local_path)
    if not p.exists():
        raise CanvasError(f"Local file not found: {local_path}")
    if not p.is_file():
        raise CanvasError(f"Path is not a file: {local_path}")

    name = display_name or p.name
    size = p.stat().st_size
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"

    # Step 1: Request an upload slot from Canvas.
    slot_data: dict[str, Any] = {"name": name, "size": size, "content_type": mime}
    if folder_path:
        slot_data["parent_folder_path"] = folder_path

    async with _canvas_client(timeout=REQUEST_TIMEOUT) as client:
        r1 = await client.post(f"{API_BASE}{path}",
                               headers=_headers(), data=slot_data)
        r1.raise_for_status()
        slot = r1.json()

    upload_url: str = slot["upload_url"]
    upload_params: dict[str, str] = {k: str(v) for k, v in
                                      slot.get("upload_params", {}).items()}
    file_param: str = slot.get("file_param", "file")

    # Step 2: POST the file to the upload URL (no auth — URL is pre-signed).
    file_bytes = p.read_bytes()
    files = {file_param: (name, file_bytes, mime)}

    async with _canvas_client(timeout=120.0, follow_redirects=False,
                              headers={"Accept": "*/*"}) as client:
        r2 = await client.post(upload_url, data=upload_params, files=files)

        if r2.status_code in (301, 302, 303):
            # Step 3: S3 path — follow redirect to Canvas confirm URL (needs auth).
            confirm_url = r2.headers["location"]
            async with _canvas_client(timeout=REQUEST_TIMEOUT) as c3:
                r3 = await c3.get(confirm_url, headers=_headers())
                r3.raise_for_status()
                return r3.json()

        r2.raise_for_status()
        return r2.json()  # Canvas-native storage returns file object directly.


async def _canvas_upload_file(course_id: int, local_path: str,
                               display_name: Optional[str],
                               folder_path: Optional[str]) -> dict[str, Any]:
    """Upload a file to Canvas course files."""
    return await _upload_file(f"/courses/{course_id}/files", local_path,
                              display_name, folder_path)


async def _upload_user_file(local_path: str,
                             display_name: Optional[str]) -> dict[str, Any]:
    """Upload a file to the authenticated user's conversation attachments."""
    # Canvas requires attachment_ids[] to reference files in this specific folder.
    return await _upload_file("/users/self/files", local_path, display_name,
                              "conversation attachments")


# --------------------------------------------------------------------------- #
# New tools: upload file, get submission, send message
# --------------------------------------------------------------------------- #

@_tool(
    name="canvas_upload_file",
    annotations={"title": "Upload File to Canvas", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def canvas_upload_file(params: UploadFileInput) -> str:
    """Upload a local file to Canvas course files. WRITE — gated by confirm.

    Returns a Canvas file_id you can pass to canvas_submit_assignment with
    submission_type='online_upload'. The first call previews the file metadata
    (name, size, MIME type, destination course); set confirm=True to upload.

    Args:
        params.course_id: Target course.
        params.local_path: Absolute path to the file on your local machine.
        params.display_name: Override the filename shown in Canvas.
        params.folder_path: Folder inside course files (default: root).
        params.confirm: Must be True to upload.

    Returns:
        str: Dry-run preview, or confirmation with the Canvas file_id.
    """
    try:
        p = _safe_path(params.local_path)
        name = params.display_name or p.name
        mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"

        if not params.confirm:
            if not p.exists():
                return f"Error: Local file not found: {params.local_path}"
            size_kb = p.stat().st_size / 1024
            folder = params.folder_path or "(course root)"
            return _dry_run(
                "upload a file to Canvas course files",
                target=f"course {params.course_id}, folder '{folder}'",
                payload=f"{name!r} · {mime} · {size_kb:.1f} KB",
            )

        file_obj = await _canvas_upload_file(
            params.course_id, params.local_path,
            params.display_name, params.folder_path,
        )
        fid = file_obj.get("id", "?")
        fname = file_obj.get("display_name") or name
        return (f"✅ Uploaded '{fname}' to course {params.course_id} "
                f"(Canvas file ID: **{fid}**). "
                f"Pass file_ids=[{fid}] to canvas_submit_assignment for online_upload.")
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_get_submission",
    annotations={"title": "Get Submission Detail", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_get_submission(params: GetSubmissionInput) -> str:
    """Get the full submission detail for one of your assignments: body, grade,
    rubric assessment scores, and all grader comments/feedback.

    Use canvas_get_assignment to see the assignment itself; use this tool to
    see what you submitted and what feedback you received.

    Args:
        params.course_id / params.assignment_id: Target assignment.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Submission body, score, rubric feedback, and grader comments.
    """
    try:
        sub = await _get(
            f"/courses/{params.course_id}/assignments/{params.assignment_id}"
            "/submissions/self",
            {"include[]": ["rubric_assessment", "submission_comments",
                           "full_rubric_assessment"]},
        )

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(sub, indent=2)

        state = sub.get("workflow_state", "unsubmitted")
        score = sub.get("score")
        pts_possible: Optional[float] = None

        # Fetch assignment for points_possible and rubric criteria.
        rubric_criteria: list[dict[str, Any]] = []
        try:
            a = await _get(f"/courses/{params.course_id}/assignments/"
                           f"{params.assignment_id}")
            pts_possible = a.get("points_possible")
            rubric_criteria = a.get("rubric") or []
        except Exception:  # noqa: BLE001
            pass

        score_str = (f"{score}/{pts_possible}" if pts_possible is not None
                     else str(score) if score is not None else "—")

        lines: list[str] = [
            f"## Submission — assignment {params.assignment_id}",
            f"- **State:** {state}",
            f"- **Score:** {score_str}",
            f"- **Submitted at:** {_fmt_dt(sub.get('submitted_at'))}",
            f"- **Graded at:** {_fmt_dt(sub.get('graded_at'))}",
            f"- **Attempt:** {sub.get('attempt', '—')}",
        ]
        if sub.get("late"):
            lines.append("- ⚠️ **Late submission**")
        if sub.get("missing"):
            lines.append("- ⚠️ **Missing**")
        if sub.get("excused"):
            lines.append("- **Excused**")

        # Submission body (text entry or URL).
        body = sub.get("body") or sub.get("preview_url")
        if body:
            lines += ["", "### Submission Body", _strip_html(body, limit=2000)]
        if sub.get("url"):
            lines += ["", "### Submitted URL", sub["url"]]

        # Rubric assessment.
        rubric_assessment = sub.get("rubric_assessment") or {}
        if rubric_assessment and rubric_criteria:
            lines += ["", "### Rubric Feedback"]
            criteria_map = {c["id"]: c for c in rubric_criteria}
            for crit_id, rating in rubric_assessment.items():
                crit = criteria_map.get(crit_id, {})
                desc = crit.get("description", crit_id)
                pts = rating.get("points", "—")
                max_pts = crit.get("points", "?")
                comment = rating.get("comments", "")
                lines.append(f"- **{desc}**: {pts}/{max_pts}"
                             + (f" — {comment}" if comment else ""))
        elif rubric_assessment:
            lines += ["", "### Rubric Assessment (raw)"]
            for crit_id, rating in rubric_assessment.items():
                lines.append(f"- {crit_id}: {rating.get('points','—')} pts"
                             + (f" — {rating.get('comments','')}"
                                if rating.get("comments") else ""))

        # Grader comments.
        comments: list[dict[str, Any]] = sub.get("submission_comments") or []
        if comments:
            lines += ["", "### Grader Comments"]
            for c in comments:
                author = c.get("author_name") or c.get("author", {}).get("display_name", "?")
                when = _fmt_dt(c.get("created_at"))
                text = _strip_html(c.get("comment"), limit=1000)
                lines.append(f"- **{author}** · {when}\n  {text}")

        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_send_message",
    annotations={"title": "Send Canvas Inbox Message", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
)
async def canvas_send_message(params: SendMessageInput) -> str:
    """Send a Canvas Inbox (Conversations) message to one or more recipients.
    WRITE — gated by confirm.

    Optionally attach files via local_paths (auto-uploaded to your Canvas
    personal files) or attachment_ids (Canvas file IDs you already have).
    The first call previews everything; set confirm=True to actually send.

    Args:
        params.recipient_ids: List of Canvas user IDs or login IDs.
        params.subject: Message subject line.
        params.body: Message body text.
        params.course_id: Optional course context (associates the thread with a course).
        params.local_paths: Local file paths to upload and attach.
        params.attachment_ids: Canvas file IDs of already-uploaded files to attach.
        params.confirm: Must be True to send.

    Returns:
        str: Dry-run preview, or confirmation with the new conversation ID.
    """
    try:
        if not params.confirm:
            ctx = f"course {params.course_id}" if params.course_id else "no course context"
            attach_lines: list[str] = []
            for lp in params.local_paths or []:
                p = _safe_path(lp)
                if not p.exists():
                    attach_lines.append(f"  • {lp} ⚠️ FILE NOT FOUND")
                else:
                    mime = mimetypes.guess_type(lp)[0] or "application/octet-stream"
                    attach_lines.append(
                        f"  • {p.name} ({mime}, {p.stat().st_size / 1024:.1f} KB)")
            for fid in params.attachment_ids or []:
                attach_lines.append(f"  • Canvas file ID {fid}")
            attach_str = (("\n" + "\n".join(attach_lines)) if attach_lines
                          else " (no attachments)")
            return _dry_run(
                "send a Canvas Inbox message",
                target=f"recipients {params.recipient_ids} ({ctx})",
                payload=(f"Subject: {params.subject!r} · "
                         f"{_excerpt(params.body, 200)}\n"
                         f"Attachments:{attach_str}"),
            )

        # Upload any local files to the user's personal Canvas files.
        all_ids: list[int] = list(params.attachment_ids or [])
        uploaded_names: list[str] = []
        for lp in params.local_paths or []:
            file_obj = await _upload_user_file(lp, None)
            fid = file_obj.get("id")
            if fid is not None:
                all_ids.append(int(fid))
            uploaded_names.append(file_obj.get("display_name") or Path(lp).name)

        data: dict[str, Any] = {
            "recipients[]": params.recipient_ids,
            "subject": params.subject,
            "body": params.body,
            "group_conversation": "false",
        }
        if params.course_id:
            data["context_code"] = f"course_{params.course_id}"
        if all_ids:
            data["attachment_ids[]"] = all_ids

        result = await _send("POST", "/conversations", data)

        # Canvas returns a list of conversation objects.
        conv_id = "?"
        if isinstance(result, list) and result:
            conv_id = result[0].get("id", "?")
        elif isinstance(result, dict):
            conv_id = result.get("id", "?")

        attach_note = (f" with {len(all_ids)} attachment(s)"
                       f" ({', '.join(uploaded_names)})" if all_ids else "")
        return (f"✅ Message sent{attach_note} (conversation ID {conv_id}). "
                f"Subject: {params.subject!r}. "
                f"Recipients: {', '.join(str(r) for r in params.recipient_ids)}.")
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


# --------------------------------------------------------------------------- #
# Course tab tools — People, Home, Conferences, Collaborations, Outcomes, Tabs
# --------------------------------------------------------------------------- #

@_tool(
    name="canvas_list_tabs",
    annotations={"title": "List Course Tabs", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_tabs(params: ListTabsInput) -> str:
    """List all visible tabs for a course (Home, Announcements, People, Files, etc.).

    Use this as a discovery tool before calling other course-specific tools — it
    tells you which tabs are actually enabled on a given Canvas instance so you
    know what content is accessible.

    Args:
        params.course_id: Canvas course ID.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Each tab's id, label, type, and URL.
    """
    try:
        tabs = await _get(f"/courses/{params.course_id}/tabs")
        if params.response_format == ResponseFormat.JSON:
            return json.dumps(tabs, indent=2)
        if not tabs:
            return f"No tabs found for course {params.course_id}."
        lines = [f"## Tabs — course {params.course_id}", ""]
        for t in tabs:
            hidden = " *(hidden)*" if t.get("hidden") else ""
            lines.append(f"- **{t.get('label','?')}** (`{t.get('id','?')}`)"
                         f"{hidden} — {t.get('html_url','')}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_get_home_page",
    annotations={"title": "Get Course Home Page", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_get_home_page(params: GetHomePageInput) -> str:
    """Get the course front/home page body as plain text.

    Many instructors post schedules, links, and key information here rather than
    in the official syllabus field. Returns a graceful message when no front page
    is set rather than an error.

    Args:
        params.course_id: Canvas course ID.

    Returns:
        str: The front page title and body, or a message if none is set.
    """
    try:
        page = await _get(f"/courses/{params.course_id}/front_page")
        title = page.get("title") or "Front Page"
        body = _strip_html(page.get("body"), limit=HTML_TEXT_LIMIT)
        updated = _fmt_dt(page.get("updated_at"))
        return (f"## {title}\n\n*Last updated: {updated}*\n\n{body}"
                if body else f"## {title}\n\n*(no content)*")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return (f"No front page is set for course {params.course_id}. "
                    "The instructor may use the Syllabus tab instead — "
                    "try canvas_get_syllabus.")
        return _handle_api_error(e)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_list_people",
    annotations={"title": "List Course People", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_people(params: ListPeopleInput) -> str:
    """List members of a course — students, teachers, TAs, and observers.

    Useful for finding instructor names and contact details, identifying TAs,
    or checking who else is enrolled. Filter by role with enrollment_type.

    Args:
        params.course_id: Canvas course ID.
        params.enrollment_type: Optional role filter: 'student', 'teacher', 'ta',
            'observer', or 'designer'. Omit for all members.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Each member's name, role, and login id.
    """
    try:
        api_params: dict[str, Any] = {
            "include[]": ["enrollments", "email"],
        }
        if params.enrollment_type:
            api_params["enrollment_type[]"] = params.enrollment_type

        users = await _paginated_get(f"/courses/{params.course_id}/users", api_params)

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(users, indent=2)
        if not users:
            return f"No members found for course {params.course_id}."

        # Group by role for readability.
        role_order = ["TeacherEnrollment", "TaEnrollment", "DesignerEnrollment",
                      "StudentEnrollment", "ObserverEnrollment"]
        role_label = {
            "TeacherEnrollment": "Instructor",
            "TaEnrollment": "TA",
            "DesignerEnrollment": "Designer",
            "StudentEnrollment": "Student",
            "ObserverEnrollment": "Observer",
        }
        grouped: dict[str, list[dict[str, Any]]] = {}
        for u in users:
            enrollments = u.get("enrollments") or []
            role = enrollments[0].get("type", "Unknown") if enrollments else "Unknown"
            grouped.setdefault(role, []).append(u)

        lines = [f"## People — course {params.course_id}", ""]
        for role in role_order:
            members = grouped.pop(role, [])
            if not members:
                continue
            lines.append(f"### {role_label.get(role, role)}s ({len(members)})")
            for u in members:
                login = u.get("login_id") or u.get("email") or "—"
                lines.append(f"- **{u.get('name','?')}** — {login} "
                             f"(ID {u.get('id','?')})")
            lines.append("")
        # Any unexpected roles
        for role, members in grouped.items():
            lines.append(f"### {role} ({len(members)})")
            for u in members:
                lines.append(f"- **{u.get('name','?')}** (ID {u.get('id','?')})")
            lines.append("")
        return "\n".join(lines).rstrip()
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_list_conferences",
    annotations={"title": "List Course Conferences", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_conferences(params: ListConferencesInput) -> str:
    """List video conference sessions for a course (BigBlueButton, Zoom, etc.).

    Shows scheduled, in-progress, and concluded sessions with join URLs where
    available. Returns a graceful message if the Conferences tab is not enabled.

    Args:
        params.course_id: Canvas course ID.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Each conference's title, status, duration, and join URL.
    """
    try:
        data = await _get(f"/courses/{params.course_id}/conferences")
        # Canvas wraps this in {"conferences": [...]}
        conferences = data.get("conferences", data) if isinstance(data, dict) else data

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(conferences, indent=2)
        if not conferences:
            return f"No conferences found for course {params.course_id}."

        lines = [f"## Conferences — course {params.course_id}", ""]
        for c in conferences:
            status = "concluded" if c.get("ended_at") else (
                "in progress" if c.get("started_at") else "scheduled")
            duration = c.get("duration")
            dur_str = f"{duration} min" if duration else "—"
            join_url = c.get("join_url") or c.get("url") or "—"
            lines += [
                f"### {c.get('title','?')}",
                f"- **Status:** {status}",
                f"- **Type:** {c.get('conference_type','?')}",
                f"- **Duration:** {dur_str}",
                f"- **Started:** {_fmt_dt(c.get('started_at'))}",
                f"- **Ended:** {_fmt_dt(c.get('ended_at'))}",
                f"- **Join URL:** {join_url}",
                "",
            ]
        return "\n".join(lines).rstrip()
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_list_collaborations",
    annotations={"title": "List Course Collaborations", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_collaborations(params: ListCollaborationsInput) -> str:
    """List collaborative documents shared in a course (Google Docs, Etherpad, etc.).

    Returns title, collaboration type, author, creation date, and the URL to
    open each document.

    Args:
        params.course_id: Canvas course ID.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Each collaboration's title, type, author, and URL.
    """
    try:
        collabs = await _paginated_get(f"/courses/{params.course_id}/collaborations")

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(collabs, indent=2)
        if not collabs:
            return f"No collaborations found for course {params.course_id}."

        lines = [f"## Collaborations — course {params.course_id}", ""]
        for c in collabs:
            author = (c.get("user", {}) or {}).get("display_name") or "?"
            url = c.get("url") or "—"
            lines += [
                f"- **{c.get('title','?')}** ({c.get('collaboration_type','?')})",
                f"  Author: {author} · Created: {_fmt_dt(c.get('created_at'))}",
                f"  URL: {url}",
                "",
            ]
        return "\n".join(lines).rstrip()
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


@_tool(
    name="canvas_list_outcomes",
    annotations={"title": "List Course Outcomes", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_list_outcomes(params: ListOutcomesInput) -> str:
    """List the learning outcomes for a course, grouped by outcome group.

    Outcomes describe the skills and knowledge students are expected to demonstrate.
    Each outcome shows its title, description, and points possible.

    Args:
        params.course_id: Canvas course ID.
        params.response_format: 'markdown' or 'json'.

    Returns:
        str: Outcome groups with each outcome's title, description, and points.
    """
    try:
        links = await _paginated_get(f"/courses/{params.course_id}/outcome_group_links")

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(links, indent=2)
        if not links:
            return f"No learning outcomes found for course {params.course_id}."

        # Group by outcome_group title.
        grouped: dict[str, list[dict[str, Any]]] = {}
        for link in links:
            group_title = (link.get("outcome_group") or {}).get("title", "Outcomes")
            outcome = link.get("outcome") or {}
            grouped.setdefault(group_title, []).append(outcome)

        lines = [f"## Learning Outcomes — course {params.course_id}", ""]
        for group_title, outcomes in grouped.items():
            lines.append(f"### {group_title}")
            for o in outcomes:
                pts = o.get("points_possible")
                pts_str = f" ({pts} pts)" if pts is not None else ""
                desc = _strip_html(o.get("description"), limit=300)
                lines.append(f"- **{o.get('title','?')}**{pts_str}")
                if desc:
                    lines.append(f"  {desc}")
            lines.append("")
        return "\n".join(lines).rstrip()
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)


class ListToolsInput(_Base):
    pass


@_tool(
    name="canvas_list_tools",
    annotations={"title": "List Available Canvas Tools", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def canvas_list_tools(params: ListToolsInput) -> str:
    """List every tool this Canvas MCP server provides, grouped by category, with
    a one-line purpose for each. Call this FIRST when exploring what this
    connector can do — courses, assignments, grades, announcements, modules,
    pages, files, discussions, quizzes, calendar, to-do, people, and the
    confirm-gated write tools all live here, so one call surfaces the whole
    toolset instead of discovering them one search at a time.

    Returns:
        str: Every tool name and a one-line description, grouped by category.
    """
    groups = [
        ("Code Mode (best for multi-step or cross-course questions)", [
            ("canvas_run_code", "Run a JavaScript snippet against a read-only Canvas API; only its printed output comes back."),
            ("canvas_run_code_write", "Same, but the script may also call the write tools (pass confirm: true to act)."),
        ]),
        ("Profile & courses", [
            ("canvas_get_profile", "Verify the token and see whose account is connected."),
            ("canvas_list_courses", "List your courses, with term and current grade."),
        ]),
        ("Assignments & grades", [
            ("canvas_list_assignments", "List a course's assignments with due dates and submission status."),
            ("canvas_get_assignment", "Full detail on one assignment: description, rubric, your submission."),
            ("canvas_get_submission", "Your submission body, score, rubric feedback, grader comments."),
            ("canvas_get_grades", "Current grade summary across courses, or per-assignment scores for one."),
            ("canvas_list_upcoming", "Everything due soon across all courses, plus an always-on overdue sweep."),
            ("canvas_list_todo", "Canvas's own to-do list of unsubmitted work."),
        ]),
        ("Announcements & communication", [
            ("canvas_list_announcements", "Recent announcements, one course or all active courses."),
            ("canvas_send_message", "Send a Canvas Inbox message (write — confirm-gated)."),
        ]),
        ("Course content", [
            ("canvas_list_modules", "A course's module structure, optionally with items."),
            ("canvas_get_syllabus", "A course's syllabus body."),
            ("canvas_list_pages", "Wiki pages in a course."),
            ("canvas_get_page", "Read one wiki page's full body."),
            ("canvas_get_home_page", "A course's front/home page body."),
            ("canvas_list_files", "Files uploaded to a course."),
            ("canvas_get_file", "A file's metadata; text and PDF content is extracted when possible."),
            ("canvas_download_file", "Save a Canvas file (syllabus PDF, slides…) to ~/Downloads/Canvas."),
        ]),
        ("Discussions & quizzes", [
            ("canvas_list_discussions", "Discussion topics in a course."),
            ("canvas_get_discussion", "Read a topic's prompt and replies (or just your own entries)."),
            ("canvas_list_quizzes", "Quizzes in a course with due dates and points."),
            ("canvas_get_quiz", "One quiz's settings and instructions."),
        ]),
        ("Calendar & course meta", [
            ("canvas_list_calendar_events", "Calendar events or assignment due-dates in a date range."),
            ("canvas_list_tabs", "Which tabs/features are enabled for a course."),
            ("canvas_list_people", "Course members — students, teachers, TAs."),
            ("canvas_list_conferences", "Video conference sessions for a course."),
            ("canvas_list_collaborations", "Shared collaborative documents in a course."),
            ("canvas_list_outcomes", "Learning outcomes for a course."),
        ]),
        ("Write tools (all confirm-gated — first call previews, second with confirm=true acts)", [
            ("canvas_create_planner_note", "Create a personal planner note/to-do."),
            ("canvas_submit_assignment", "Submit text, a URL, or files from this computer to an assignment."),
            ("canvas_post_discussion_entry", "Post a new top-level discussion entry."),
            ("canvas_reply_to_entry", "Reply to a specific discussion entry."),
            ("canvas_post_submission_comment", "Comment on your own submission."),
            ("canvas_upload_file", "Upload a local file to Canvas course files."),
        ]),
    ]
    lines = ["# Canvas MCP — available tools\n"]
    for title, tools in groups:
        lines.append(f"## {title}")
        for name, desc in tools:
            lines.append(f"- **{name}** — {desc}")
        lines.append("")
    return "\n".join(lines).rstrip()


# --------------------------------------------------------------------------- #
# Code Mode — run model-written JavaScript against a read-only Canvas API
# --------------------------------------------------------------------------- #
# Idea from https://blog.cloudflare.com/code-mode/: instead of chaining many
# tool calls (each result round-tripping through the model's context), the model
# writes one script that calls the API, filters/joins the data, and prints only
# the answer. The script runs in Deno with zero permissions (sandbox_runner.js);
# every Canvas call is proxied back here, GET-only, so the token never enters
# the sandbox and writes still go through the confirm-gated tools.

SANDBOX_RUNNER = Path(__file__).resolve().parent / "sandbox_runner.js"
CODE_TIMEOUT = 120.0      # wall-clock seconds for one script
CODE_MAX_CALLS = 300      # Canvas calls one script may make
CODE_OUTPUT_LIMIT = 30000  # characters of output returned to the model


def _deno_path() -> Optional[str]:
    """Locate deno; GUI-launched clients often have a PATH without Homebrew."""
    import shutil
    candidates = [os.environ.get("DENO_PATH"), shutil.which("deno"),
                  "/opt/homebrew/bin/deno", "/usr/local/bin/deno",
                  str(Path.home() / ".deno/bin/deno")]
    return next((c for c in candidates if c and Path(c).is_file()), None)


def _api_path(path: Any) -> str:
    """Validate a sandbox-supplied Canvas API path (relative to /api/v1)."""
    if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
            or ".." in path or "://" in path or "\\" in path):
        raise CanvasError(f"path must look like '/courses/123/assignments', got {path!r}")
    return path


async def _code_file_text(file_id: Any, limit: Any) -> Optional[str]:
    """Text of a Canvas file (PDF text layer or text-like file); None if binary."""
    f = await _get(f"/files/{int(file_id)}")
    url, size = f.get("url"), f.get("size")
    name = f.get("display_name") or f.get("filename") or ""
    ctype = str(f.get("content-type") or f.get("content_type") or "").lower()
    if not url:
        return None
    if "pdf" not in ctype and not name.lower().endswith(".pdf"):
        return await _fetch_text(url, limit=int(limit))
    if isinstance(size, int) and size > 15 * 1024 * 1024:
        raise CanvasError(f"PDF {name} is over 15 MB; not extracted")
    return _extract_pdf_text(await _download_bytes(url), limit=int(limit))


async def _code_tool(name: Any, args: Any, allow_writes: bool) -> str:
    """Run one of this server's tools from inside a script. Write tools only
    when the script came through canvas_run_code_write."""
    fn, read_only = _TOOLS.get(str(name), (None, False))
    if fn is None or str(name).startswith("canvas_run_code"):
        raise CanvasError(f"{name!r} is not a Canvas tool callable from a script")
    if not allow_writes and not read_only:
        raise CanvasError(f"{name!r} writes to Canvas; call it as its own tool instead")
    model = inspect.signature(fn).parameters["params"].annotation
    return await fn(model(**(args or {})))


async def _code_dispatch(method: str, args: list[Any], allow_writes: bool) -> Any:
    if method == "get":
        return await _get(_api_path(args[0]), args[1])
    if method == "getAll":
        return await _paginated_get(_api_path(args[0]), args[1],
                                    max_items=min(int(args[2]), 2000))
    if method == "courses":
        return await _fetch_courses("active")
    if method == "fileText":
        return await _code_file_text(args[0], args[1])
    if method == "htmlToText":
        return _strip_html(args[0], limit=int(args[1]))
    if method == "course":
        return await _resolve_course(args[0])
    if method == "tool":
        return await _code_tool(args[0], args[1], allow_writes)
    raise CanvasError(f"unknown canvas method {method!r}")


def _rpc_error(e: Exception) -> str:
    return _handle_api_error(e).removeprefix("Error: ")


async def _run_sandbox(code: str, allow_writes: bool = False) -> dict[str, Any]:
    """Run `code` in the Deno sandbox, serving its canvas.* calls. Returns the
    runner's final message: {logs, result, error}."""
    deno = _deno_path()
    if not deno:
        raise CanvasError("Code Mode needs Deno. Install it with `brew install deno` "
                          "(or set DENO_PATH), then restart the MCP server.")
    env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "NO_COLOR": "1",
           "DENO_NO_UPDATE_CHECK": "1"}  # deliberately no CANVAS_API_TOKEN
    proc = await asyncio.create_subprocess_exec(
        deno, "run", "--no-prompt", "--no-remote", "--no-npm", "--no-config",
        "--no-lock", "--v8-flags=--max-old-space-size=256", str(SANDBOX_RUNNER),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env, limit=32 * 1024 * 1024)
    assert proc.stdin and proc.stdout and proc.stderr
    stderr_task = asyncio.create_task(proc.stderr.read())
    sem = asyncio.Semaphore(SWEEP_CONCURRENCY)
    calls = 0
    tasks: set[asyncio.Task] = set()

    def reply(msg: dict[str, Any]) -> None:
        if not proc.stdin.is_closing():
            proc.stdin.write((json.dumps(msg, default=str) + "\n").encode())

    async def serve(msg: dict[str, Any]) -> None:
        try:
            async with sem:
                value = await _code_dispatch(msg.get("m", ""), list(msg.get("a") or []),
                                             allow_writes)
            reply({"id": msg["id"], "ok": True, "v": value})
        except Exception as e:  # noqa: BLE001
            reply({"id": msg["id"], "ok": False, "e": _rpc_error(e)})

    async def pump() -> dict[str, Any]:
        nonlocal calls
        proc.stdin.write((json.dumps({"code": code, "tz": LOCAL_TZ_NAME}) + "\n").encode())
        while line := await proc.stdout.readline():
            msg = json.loads(line)
            if msg.get("t") == "done":
                return msg
            calls += 1
            if calls > CODE_MAX_CALLS:
                reply({"id": msg["id"], "ok": False,
                       "e": f"call limit reached ({CODE_MAX_CALLS} per script)"})
                continue
            task = asyncio.create_task(serve(msg))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        err = (await stderr_task).decode(errors="replace").strip()
        raise CanvasError(f"sandbox exited without a result: {err[-1500:] or 'no output'}")

    try:
        result = await asyncio.wait_for(pump(), CODE_TIMEOUT)
        result["calls"] = calls
        return result
    except asyncio.TimeoutError:
        raise CanvasError(f"script timed out after {CODE_TIMEOUT:.0f}s") from None
    finally:
        for t in tasks:
            t.cancel()
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        stderr_task.cancel()


class RunCodeInput(_Base):
    code: str = Field(..., min_length=1, max_length=50000,
                      description="Body of an async JavaScript function. Use `await canvas.*`, "
                                  "console.log what you need, and/or `return` a JSON value.")


@_tool(
    name="canvas_run_code",
    annotations={"title": "Run Code Against Canvas (read-only)", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def canvas_run_code(params: RunCodeInput) -> str:
    """Code Mode: run JavaScript against a read-only Canvas API. Prefer it for
    anything needing several calls, several courses, or filtering/sorting —
    only what you console.log or return comes back.

    `code` is an async function body (await and `return` work). Plain JS, no
    imports; sandboxed with no network/files/env. Its only API:

    ```ts
    declare const canvas: {
      tz: string;                                          // e.g. "Asia/Manila"
      get(path: string, params?: object): Promise<any>;    // GET /api/v1{path}, e.g. {"include[]": ["submission"]}
      getAll(path: string, params?: object, maxItems?: number): Promise<any[]>;  // follows pagination
      courses(): Promise<any[]>;                           // active courses: id, name, course_code, term
      course(ref: string | number): Promise<number>;       // "THEO 13" -> id; throws if ambiguous
      fileText(fileId: number, limit?: number): Promise<string | null>;  // PDF/text content
      htmlToText(html: string, limit?: number): Promise<string>;
      tool(name: string, args?: object): Promise<string>;  // run a read tool below, returns Markdown
      fmt(iso: string | null): string;                     // "YYYY-MM-DD HH:MM" local
    };
    ```

    Read tools for canvas.tool() (args = their usual params; course_id takes a
    name): canvas_list_upcoming {days_ahead} (includes overdue + graded
    discussions), canvas_list_todo, canvas_get_grades {course_id?},
    canvas_get_assignment {course_id, assignment_id}, canvas_get_submission,
    canvas_list_announcements, canvas_get_syllabus, canvas_get_discussion
    {course_id, topic_id, only_mine?}, canvas_list_modules, canvas_get_page,
    canvas_get_file, canvas_list_people, canvas_list_calendar_events, and the
    other canvas_list_*/canvas_get_* tools.

    Endpoints: /courses/:id/assignments, /courses/:id/assignment_groups
    (include[]=assignments), /courses/:id/discussion_topics,
    /announcements?context_codes[]=course_:id, /courses/:id/modules?include[]=items,
    /courses/:id/pages/:url, /courses/:id/quizzes, /planner/items, /conversations.

    Example: `const id = await canvas.course("THEO 13"); const as = await
    canvas.getAll(`/courses/${id}/assignments`, {bucket: "upcoming"}); return
    as.map(a => `${a.name} — ${canvas.fmt(a.due_at)}`);`

    No writes here — use the canvas_* write tools. Limits: 120 s, 300 calls,
    30,000 output chars. Failed canvas.* calls throw.

    Returns:
        str: Captured console output, then the returned value as JSON.
    """
    return await _code_output(params.code, allow_writes=False)


@_tool(
    name="canvas_run_code_write",
    annotations={"title": "Run Code Against Canvas (can write)", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
)
async def canvas_run_code_write(params: RunCodeInput) -> str:
    """Same sandbox and `canvas` API as canvas_run_code, but canvas.tool() may
    also call the write tools, so one script can read and then act — e.g. find
    every unanswered graded discussion and post the entries the user approved.

    Write tools available through canvas.tool(name, args): canvas_submit_assignment,
    canvas_post_discussion_entry, canvas_reply_to_entry,
    canvas_post_submission_comment, canvas_send_message, canvas_upload_file,
    canvas_create_planner_note, canvas_download_file (saves to this computer). Args are each tool's usual params. Without
    `confirm: true` a write tool returns a preview and sends nothing; with it,
    the write happens. Course args accept codes/names like "THEO 13".

    Use canvas_run_code for anything read-only. Before confirming writes, show
    the user what will be written unless they already approved those exact
    actions. Nothing can delete. Same limits as canvas_run_code.

    Returns:
        str: Captured console output, then the returned value as JSON.
    """
    return await _code_output(params.code, allow_writes=True)


async def _code_output(code: str, allow_writes: bool) -> str:
    try:
        res = await _run_sandbox(code, allow_writes)
    except Exception as e:  # noqa: BLE001
        return _handle_api_error(e)
    parts = list(res.get("logs") or [])
    if res.get("result") is not None:
        r = res["result"]
        parts.append(r if isinstance(r, str) else json.dumps(r, indent=1, ensure_ascii=False))
    if res.get("error"):
        parts.append(f"Error in script: {res['error']}")
    text = "\n".join(parts).strip() or "(script produced no output — console.log or return something)"
    if len(text) > CODE_OUTPUT_LIMIT:
        text = (text[:CODE_OUTPUT_LIMIT].rstrip()
                + f"\n…[truncated at {CODE_OUTPUT_LIMIT} characters — print less]")
    return text


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Canvas LMS MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="MCP transport to run. Use streamable-http for ChatGPT connectors.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_HOST", mcp.settings.host),
        help="Host for HTTP transports.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_PORT", mcp.settings.port)),
        help="Port for HTTP transports.",
    )
    parser.add_argument(
        "--path",
        default=os.environ.get("MCP_PATH", mcp.settings.streamable_http_path),
        help="Path for streamable HTTP transport.",
    )
    parser.add_argument(
        "--dev-allow-tunnel-hosts",
        action="store_true",
        default=os.environ.get("MCP_DEV_ALLOW_TUNNEL_HOSTS", "").lower() in {"1", "true", "yes"},
        help="Disable DNS rebinding protection for local development through HTTPS tunnels.",
    )
    args = parser.parse_args()

    if args.transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.settings.streamable_http_path = args.path
        if args.dev_allow_tunnel_hosts:
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=False,
            )

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
