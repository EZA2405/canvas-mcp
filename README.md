# Canvas LMS MCP Server

A local MCP server that gives Claude access to your Canvas (Ateneo
`ateneo.instructure.com`) account: courses, assignments, deadlines, grades,
announcements, modules, syllabi, **pages, files, discussions, quizzes, calendar,
and to-do** — plus a small set of **confirm-gated write actions** (submit an
assignment, post/reply on a discussion, comment on a submission, create a
planner note).

## ⚠️ Before anything else: rotate your token

If you have ever pasted your token into a chat, treat it as compromised:

1. Canvas → **Account → Settings → Approved Integrations**.
2. Delete the old token.
3. **+ New Access Token** → copy the new value.
4. Put the new value only in the config below — never in chat or in the code.

## How writes stay safe (read this)

Five tools can change things on your Canvas account. Every one of them is gated
behind a **`confirm` flag that defaults to `False`**:

- Call it normally → it **sends nothing** and returns a *preview* of exactly what
  it would do (which assignment, due date, your current submission state, an
  excerpt of the content).
- Only a second call with **`confirm=true`** actually performs the write.

So a single ambiguous request can never submit homework or post to a class
discussion under your name by accident. **No tool deletes anything** — deletion
was deliberately left out.

## Read tools

| Tool | Purpose |
|------|---------|
| `canvas_get_profile` | Verify the token / who's connected |
| `canvas_list_courses` | Active, completed, or all courses with current grades |
| `canvas_list_assignments` | Assignments for a course + submission status (filter by bucket) |
| `canvas_get_assignment` | Full detail of one assignment — description, **full rubric (criteria, rating scales, your per-criterion score)**, points |
| `canvas_list_upcoming` | Everything due in the next N days across all courses |
| `canvas_get_grades` | Grade summary across courses, or per-assignment scores |
| `canvas_list_announcements` | Recent announcements, one course or all |
| `canvas_list_modules` | Course module structure + items (now shows page slugs / content IDs) |
| `canvas_get_syllabus` | A course's syllabus field as text |
| `canvas_list_pages` | List wiki Pages (title, slug, page ID) — find syllabi/schedules stored as Pages |
| `canvas_get_page` | Read a Page's full body as text |
| `canvas_list_files` | List course files (name, ID, type, size) |
| `canvas_get_file` | File metadata; inlines text for text-based files, download URL for binaries |
| `canvas_download_file` | Save a Canvas file to `~/Downloads/Canvas` (or `CANVAS_DOWNLOAD_DIR` / `dest_dir`); never overwrites |
| `canvas_list_discussions` | List discussion topics in a course |
| `canvas_get_discussion` | A topic's prompt + full threaded replies |
| `canvas_list_quizzes` | List quizzes with due dates, points, question counts |
| `canvas_get_quiz` | Quiz settings + instructions |
| `canvas_list_calendar_events` | Calendar events or assignment due-dates in a date range |
| `canvas_list_todo` | Canvas's native to-do list |

## Code Mode (`canvas_run_code`)

Based on Cloudflare's [Code Mode](https://blog.cloudflare.com/code-mode/). For
questions that need many Canvas calls ("which unsubmitted assignments worth 20+
points are due in the next two weeks, across all courses?"), Claude writes one
short JavaScript snippet instead of chaining a dozen tool calls. The snippet
runs on your machine, calls Canvas, filters the data, and returns only the
answer — faster, and it keeps large raw data out of Claude's context.

- Runs in [Deno](https://deno.com) with **no permissions**: no internet, no
  files, no environment variables, no other programs. Install once with
  `brew install deno` (or set `DENO_PATH`).
- Its only access is a `canvas` object that this server answers. Requests are
  **GET-only** and limited to the Canvas API, and your token never enters the
  sandbox.
- `canvas_run_code` is read-only. `canvas_run_code_write` uses the same sandbox
  but may also call the write tools below from the script (for example, find
  every unanswered graded discussion, then post). It is marked destructive, so
  Claude asks your approval before each run. Each write still previews unless
  the script passes `confirm: true`. Nothing can delete.
- Limits per script: 120 seconds, 300 Canvas calls, 30,000 characters of output.

## Local file and network safety

- Tools read or write local files only inside `~/Downloads`, `~/Desktop`, and
  `~/Documents` (override with `CANVAS_FILE_ROOTS`, colon-separated). Hidden
  files and folders (`.env`, `.ssh`, …) and this server's own folder are always
  refused. Symlinks are resolved before checking.
- The ChatGPT quick tunnel (`scripts/start_chatgpt_canvas_mcp.sh`) serves the
  MCP endpoint at `/mcp/<random secret>`, regenerated on every start. Every
  other path returns 404. Treat the printed URL like a password.
- `.runtime/` (tunnel logs, PIDs, the secret) is git-ignored.

## Lean mode (`CANVAS_TOOLSET=lean`)

`CANVAS_TOOLSET=lean` is set in `.env`, so every client gets it unless its
own env block overrides it (Claude Desktop rewrites its config file and drops
added keys, so it can't be set there). It exposes only 9 tools:
read-only `canvas_run_code` plus the individual write tools (download, planner
note, submit, post, reply, comment, upload, message). Every other read tool stays
reachable from scripts via `canvas.tool(...)`. Tool definitions drop from about
15,000 to about 3,700 tokens per chat, and no script ever needs write approval.
To give one client the full set, add `CANVAS_TOOLSET=full` to its env block.

## Course names instead of IDs

Every `course_id` (and `course_ids` / `exclude_course_ids`) accepts a numeric
ID **or** a course code or name: `"THEO 13"`, `"theo13"`, `"FAA 111.20"`,
`"philosophy of religion"`. Active courses are searched first, then completed
ones. If a name fits more than one course (e.g. `"PE"`), the tool lists the
matches instead of guessing.

## Write tools (confirm-gated)

| Tool | Purpose | Stakes |
|------|---------|--------|
| `canvas_create_planner_note` | Create a personal planner note / to-do | Low (private, reversible) |
| `canvas_submit_assignment` | Submit text, a URL, or uploaded file IDs to an assignment | **High (graded)** |
| `canvas_post_discussion_entry` | Post a new top-level entry to a discussion | **Public to class** |
| `canvas_reply_to_entry` | Reply to a specific discussion entry | **Public to class** |
| `canvas_post_submission_comment` | Comment on your own submission (note to grader) | Medium |

`canvas_submit_assignment` supports `online_text_entry` (`body`), `online_url`
(`url`), and `online_upload` (`local_paths` — files on this computer, uploaded
to the assignment only on the `confirm=true` call; `file_ids` also accepted).

## Setup

### 1. Get the tools and a Canvas token

- Install [uv](https://docs.astral.sh/uv/) (it installs Python for you):
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
  (Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`).
- Optional, for Code Mode: `brew install deno`.
- In Canvas: **Account → Settings → Approved Integrations → + New Access Token**.
  Copy the token. Never paste it into a chat.

### 2. Add the server to your MCP client

**Claude Desktop** — open the config file:
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Add this (merge into `mcpServers` if the file already has servers):

```json
{
  "mcpServers": {
    "canvas": {
      "command": "/Users/YOU/.local/bin/uvx",
      "args": ["--from", "git+https://github.com/EZA2405/canvas-mcp", "canvas-mcp"],
      "env": {
        "CANVAS_API_TOKEN": "PASTE_YOUR_TOKEN_HERE",
        "CANVAS_BASE_URL": "https://ateneo.instructure.com"
      }
    }
  }
}
```

- Replace `/Users/YOU/.local/bin/uvx` with the output of `which uvx`
  (Windows: `where uvx`). Claude Desktop does not see your shell `PATH`.
- Change `CANVAS_BASE_URL` if your school uses another Canvas instance.

**Claude Code:**

```bash
claude mcp add canvas -s user -e CANVAS_API_TOKEN=PASTE_YOUR_TOKEN_HERE -- uvx --from git+https://github.com/EZA2405/canvas-mcp canvas-mcp
```

**Update to the latest version:** `uv cache clean canvas-mcp`, then restart the client.

### Developing locally

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Point your client at `./venv/bin/python /ABSOLUTE/PATH/canvas_mcp.py` instead
(see `claude_desktop_config.example.json`).

### 3. Restart your client

Try: *"Use Canvas to read the DECSC 25 syllabus page,"* then *"What's on my
calendar this week?"*

## Test it manually first (recommended for the write tools)

```bash
CANVAS_API_TOKEN="your_new_token" \
CANVAS_BASE_URL="https://ateneo.instructure.com" \
npx @modelcontextprotocol/inspector uvx --from git+https://github.com/EZA2405/canvas-mcp canvas-mcp
```

In the Inspector, call a write tool **without** `confirm` first and read the
preview before ever setting `confirm=true`.

## Config reference

| Env var | Required | Default |
|---------|----------|---------|
| `CANVAS_API_TOKEN` | yes | — |
| `CANVAS_BASE_URL` | no | `https://ateneo.instructure.com` |

## Notes

- Pagination is handled automatically (up to a safety cap).
- `canvas_list_upcoming` and the planner note tool use the Canvas Planner API; if
  your instance has it disabled, fall back to `canvas_list_assignments` with
  `bucket: upcoming`.
- `canvas_list_calendar_events` caps contexts at 10 per request (a Canvas limit);
  with many active courses, pass a `course_id` to scope it.
- Quiz *questions* are generally not retrievable via the API outside an attempt,
  so `canvas_get_quiz` returns settings and instructions only.
