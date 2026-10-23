# STATE

## 2026-10-23 (night) — Lean mode, trimmed definitions, connection reuse

### Changed

- `CANVAS_TOOLSET=lean` registers only `canvas_run_code` and the
  non-read-only tools except `canvas_run_code_write`. `_tool()` now keeps its
  own `_TOOLS` registry (name -> wrapper, read_only), so `canvas.tool()`
  reaches every read tool in either mode. This also removed the dependency on
  FastMCP's private `_tool_manager`.
- Tool descriptions sent to the model drop docstring `Args:`/`Returns:`
  sections (`_model_description`), and schemas drop auto-generated titles
  (`_drop_titles`). The `canvas_run_code` description was rewritten compactly
  and now lists the read tools scripts can call.
- `_get`, `_paginated_get`, and `_send` share one keep-alive httpx client per
  event loop (`_api_client`). Download and upload clients are unchanged.
- Claude Desktop config: added `CANVAS_TOOLSET=lean` to the canvas env block.
  Claude Code still uses full.

### Verified

- Tool-definition tokens: before 14,955 (37 tools); full now 11,475; lean
  3,727 (9 tools).
- Over MCP: 8 sequential tool calls took 2.73 s before and 1.66 s now. The
  13-course Code Mode sweep took 1.36 s before and 1.06 s now.
- Lean, live: hidden read tools ran from a script with name resolution, and a
  write tool's dry run worked with `course_id: "THEO 13"`.
- 18 tests pass (`tests.test_toolset` is new: lean tool set, trimmed
  descriptions, script access to hidden read tools).

## 2026-10-23 (evening) — Downloads, file submission, speed

### Changed

- New `canvas_download_file`: saves a Canvas file to `~/Downloads/Canvas`
  (`CANVAS_DOWNLOAD_DIR` or `dest_dir` override). It streams to a `.part`
  file, then renames, and never overwrites: a clash gets " (2)". It is not
  read-only (it writes local disk), so Code Mode can reach it only via
  `canvas_run_code_write`.
- `canvas_submit_assignment` takes `local_paths` for `online_upload`. Files
  are checked in the preview and uploaded via
  `/courses/:id/assignments/:id/submissions/self/files` only on
  `confirm=true`. Before this, file submission needed pre-uploaded IDs, and
  the only upload tool targets course Files, which students usually get 403 on.
- PDF text uses pypdf layout mode with whitespace collapsed, falling back to
  plain mode. Ateneo syllabi used to come out one word per line.

### Verified live

- Downloaded "ITENT Capstone Syllabus.pdf" (238 KB, 8 pages) in 2.9 s;
  `file` confirms a valid PDF. The September inst-fs HTTP 406 did not recur.
- `canvas_get_file` PDF text is readable now.
- Over MCP stdio: `canvas_download_file` saved the ITENT 122 syllabus (228 KB,
  7 pages) in 3.9 s. With the user's approval, `canvas_send_message`
  (`course_id: "ITENT 122"`, `local_paths`) sent it to Alcedrei Bangcola
  (user 59867) as conversation 1755365. Reading the conversation back
  confirmed the course context, the body, and the 228 KB attachment, so the
  upload-and-attach path works live.
- Speed over MCP: a cross-course upcoming-assignment sweep took 14 classic
  calls and 5.6 s of server time (40 s in a run with one timeout), about
  645 tokens of results. Code Mode took 1 call, 1.4 s, about 170 tokens.
  The classic path also pays model latency for each of its 14 turns.
- 15 offline tests pass (`tests.test_files` is new).

### Not verified / notes

- A live file submission was not attempted, because it would hand in graded
  work. The mocked test covers preview, upload, and submit.
- Many courses return 403 on `/courses/:id/files` (Files tab hidden). Their
  syllabi live in modules or pages, so look there.
- The earlier "Canvas unreachable" periods were the Mac's DNS failing (60 s
  lookups, even to 1.1.1.1), plus this shell's sandbox. Canvas itself was up.

## 2026-10-23 (later) — Course names and write-capable Code Mode

### Changed

- Every `course_id`, `course_ids`, and `exclude_course_ids` field is
  `CourseRef` (int or string). All tools register through `_tool()`, which
  resolves strings via `_resolve_course` before the tool body runs. Matching
  checks active courses, then completed ones, with a 10-minute cache. Exact
  code/name matches win. Otherwise every query token must match: digits whole
  ("13" never matches "131"), words as prefixes. Ambiguity returns an error
  listing the candidates.
- New tool `canvas_run_code_write` (`destructiveHint: true`): same sandbox, but
  `canvas.tool()` may call write tools. Writes still need `confirm: true`;
  nothing deletes. `canvas_run_code` stays read-only so reads need no approval.
- Sandbox API gained `canvas.course(ref)`.

### Verified

- Live, before Canvas went unreachable: "THEO 13", "theo13", "FAA 111.20",
  "itent 120", "philosophy of religion", and "swimming" resolved correctly.
  "PE" returned the ambiguity list. `canvas_list_assignments` and
  `canvas_list_announcements` worked with names.
- Over MCP: 36 tools listed and the course_id schema shows int-or-string.
  Measured tool definitions are about 14.6k tokens in total; the two code
  tools are about 1.4k.
- 11 unit tests pass, including resolver ambiguity, write-tool blocking in
  read mode, and write-mode dry-run.

- Live write verified 2026-10-23 over MCP stdio with a minimal PATH. A
  `canvas_run_code_write` script with `course_id: "THEO 13"` returned the dry
  run first, then with `confirm: true` created planner note 39387 for
  2026-10-24 on course 70437. A read-back via `canvas_run_code` confirmed it.
  The note is still on the account; delete it from the Canvas planner.

### Not verified

- The live classic-vs-Code-Mode timing comparison was not run because Canvas
  was unreachable at the time.

## 2026-10-23 — Code Mode (`canvas_run_code`)

### Changed

- New read-only tool `canvas_run_code`, modeled on Cloudflare's Code Mode. The
  model writes a JavaScript function body; `sandbox_runner.js` runs it in Deno
  with no `--allow-*` flags. Its `canvas.*` calls (`get`, `getAll`, `courses`,
  `fileText`, `htmlToText`, `tool`, `fmt`) are JSON-RPC over stdio to the
  Python server, which validates the path and makes GET-only Canvas requests.
  The token is not in the sandbox's environment. `canvas.tool` runs only tools
  annotated `readOnlyHint`.
- User code is evaluated via `AsyncFunction`, not written into the main module:
  Deno preloads string-literal `import()` calls in a main module without a read
  permission check. Inside eval'd code those imports are permission-checked.
- Deno is located via `DENO_PATH`, `PATH`, then Homebrew/`~/.deno` fallbacks,
  because GUI clients launch with a minimal PATH.

### Verified

- Live: profile, a 13-course upcoming-assignment sweep (1.8 s, one tool call),
  assignment-group weights, and `canvas.tool("canvas_list_upcoming")`.
- Over MCP stdio with `PATH=/usr/bin:/bin`: 35 tools listed, `canvas_run_code`
  returns real data.
- Blocked: network `fetch`, file reads, local and remote `import()`, env
  access, subprocesses, non-API paths, `..` traversal, write tools, and calls
  past the call limit. `tests/test_code_mode.py` covers these cases, with
  Canvas mocked.
- Run tests with `PYTHONPATH=. ./venv/bin/python -m unittest tests.test_code_mode tests.test_uploads`.

### Not done

- The existing 34 tools are unchanged. Clients still load every tool definition
  at startup.

## 2026-09-16 — Claude Desktop/Cowork proxy compatibility

### Changed

- The server loads `.env` from the directory containing `canvas_mcp.py` before
  deriving any configuration constants. Existing process environment values
  take precedence, so Codex/Claude per-server env blocks still override `.env`.
- All Canvas HTTP clients are created through `_canvas_client()`, which defaults
  `trust_env` off via `CANVAS_TRUST_ENV=0`. This prevents Claude's inherited
  `HTTP_PROXY`/`HTTPS_PROXY` values from silently routing Canvas traffic through
  an allow-list proxy. Set `CANVAS_TRUST_ENV=1` only when that proxy is intended.
- API clients send `Accept: application/json` and a stable User-Agent. Signed
  download and presigned upload clients override Accept with `*/*`; the two
  upload clients retain `follow_redirects=False`, so their explicit Canvas
  three-step upload confirmation flow is unchanged.
- Unexpected HTTP errors include up to 300 characters of response text. HTTP
  406 identifies an egress proxy as the likely source, and proxy-enabled
  connection failures now give the same actionable bypass guidance.
- `.env` and timestamped backup files are ignored by Git. The live `.env` is
  mode 0600 and is intentionally not committed.

### Verified live over MCP stdio

- `canvas_list_courses`, `canvas_get_profile`, `canvas_list_todo`, and
  `canvas_list_announcements` all returned real Canvas data.
- With bogus `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` values and
  `CANVAS_TRUST_ENV` unset, `canvas_get_profile` still reached Canvas and
  returned HTTP 200 (the `.env` default is used). With `CANVAS_TRUST_ENV=1`, the
  same MCP call failed loudly with the proxy-specific connection message.
- Claude Desktop's `canvas` entry launches this repository's venv and script.
  Its token and base URL exactly match `.env`; those two Claude-provided values
  are live because environment variables win. `.env` supplies timezone and
  `CANVAS_TRUST_ENV=0` because Claude does not set them.
- The exact Codex and Claude Desktop command/argument/env configurations were
  each used to launch the shared server over stdio. Both returned a real Canvas
  profile; the Claude launch also succeeded with bogus inherited HTTP, HTTPS,
  and ALL proxy variables while `CANVAS_TRUST_ENV` was left unset.
- `canvas_mcp.py` compiles cleanly under the project venv.

### Still unverified / external issue

- `canvas_get_file` returned real PDF metadata, and redirects correctly reached
  Canvas user-content storage and then `inst-fs-sin-prod.inscloudgate.net`.
  Eight real PDFs were attempted, but that final nginx endpoint returned HTTP
  406 every time, including independent probes using default httpx, wildcard,
  browser-like, and curl-like headers. PDF bytes/text extraction therefore
  could not be verified. This 406 was reproduced outside Claude's launch
  environment with proxy inheritance disabled, so it is separate from the
  Claude egress-proxy failure.
- Uploads were reviewed but not performed because that would write user data to
  Canvas. Both upload paths still disable automatic redirects for the presigned
  POST and explicitly follow 301/302/303 to Canvas's authenticated confirmation
  endpoint.

### Configuration recommendation

- Remove `CANVAS_API_TOKEN` from `~/.codex/config.toml` after restarting and
  smoke-testing Codex with this checkout. This reduces duplicate plaintext
  secret copies; `.env` then becomes the single local secret source. If `.env`
  is deleted or unreadable and no client supplies the token, every authenticated
  tool returns the explicit `CANVAS_API_TOKEN is not set` error. Claude currently
  remains operational in that scenario because its own env block still contains
  the matching token; Codex would not after its duplicate is removed.

## Done
All 11 issues reported about the Canvas MCP server are fixed in the working tree
(not committed yet — waiting on your go-ahead). Verified by running the actual
code with simulated Canvas responses (no live token was available in this
session) and by `python3 -m py_compile canvas_mcp.py` (clean).

- **Overdue work no longer hidden.** "What's coming up" used to ask Canvas only
  for things due from today onward, so anything already late silently vanished.
  It now always does a separate sweep for late, unsubmitted work and shows it in
  its own "⚠️ Overdue" section at the top, no matter how far ahead you ask it to
  look.
- **Graded discussion posts now show up.** Canvas does not reliably list graded
  discussion topics (e.g. "Reflection Post 2") in its planner or to-do feeds.
  Both the upcoming list and the to-do list now check each course's discussions
  directly and fold the unfinished ones in.
- **"Unsubmitted" filter fixed.** Canvas's own unsubmitted filter drops
  assignments with no online submission method (an oral exam, a paper handed in
  physically). The tool now pulls the full list and decides "unsubmitted"
  itself, so those reappear.
- **Grades no longer say "no grade posted" when work is actually graded.** Some
  instructors hide the overall course score. Where that happens, the grade
  summary now shows an estimate built from individually graded items, clearly
  labelled as an estimate and unweighted (it ignores category weightings — a
  ballpark, not the official grade).
- **Discussion replies no longer fail outright on a 403.** If Canvas refuses the
  replies endpoint (observed on this account) the tool now falls back to
  showing just the prompt with a note, instead of erroring out completely.
- **New "have I already posted?" check.** `canvas_get_discussion` has an
  `only_mine` option that shows just your own entries in a thread.
- **PDFs are now readable.** Assignment instructions, syllabi, and worksheets
  at Ateneo are almost always PDFs. `canvas_get_file` now extracts and shows
  the text layer (capped at 15 MB, best-effort — scanned/image PDFs still fall
  back to "download it yourself").
- **Empty syllabus now points you at the real one.** When the official
  syllabus field is blank, the tool searches course Files for anything named
  "syllabus" and lists it (so you can pull the PDF via the fix above).
- **Course list junk reduced.** `canvas_list_courses` can now filter to the
  current term (drops old PE courses from past school years) and/or an exact
  term name. Permanent hub sites with no term dates aren't caught by the date
  filter — filter those out by name with the new `term_name` option instead.
- **Announcements sweep is now controllable.** You can target specific course
  IDs, exclude noisy hub sites (e.g. a weather-notice site), and adjust how
  much of each announcement's body comes back, instead of always pulling 40
  full-length announcements from everywhere.
- **Times are shown in Manila time, not UTC.** Every date/time now converts to
  `Asia/Manila` (configurable via `CANVAS_TIMEZONE` env var) instead of
  requiring a mental +8h conversion.
- **One-call tool discovery.** New `canvas_list_tools` lists every tool this
  server provides, grouped by category, so finding "the courses tool" doesn't
  take five separate searches.

### Second review round — 5 further fixes (also uncommitted)
- **"Have I posted yet?" no longer lies.** If a moderator deleted a post you had
  replied under, your own reply became invisible and the tool would say you had
  not posted. It now looks inside deleted posts for live replies.
- **Duplicate items in the upcoming/to-do lists.** The same assignment coming
  from two different Canvas feeds was matched by comparing its title text, which
  breaks on a stray space or a truncated name. It now matches on Canvas's own
  numeric ID.
- **The sweeps are much faster.** The per-course requests used to run one after
  another; they now run at the same time, and the course list is fetched once
  instead of twice. Same data, fewer round trips of waiting.
- **Broken PDFs no longer blamed on scanning.** A corrupt or password-protected
  PDF used to produce the message "this is likely a scanned PDF." Those two
  cases are now told apart and worded honestly.
- **No more silent gaps.** If one course's data fails to load mid-sweep, the
  output now says which course could not be checked instead of quietly
  returning an incomplete list.

### Third review round — 3 more fixes (also uncommitted)
- **A remaining duplicate.** The round-2 ID fix missed one case: when Canvas's
  own "what's coming up" feed happens to include a graded discussion itself
  (it sometimes does), it was tagged with a different internal ID than the
  same discussion coming from our own sweep, so it could still show up twice.
  Both now use the same ID.
- **The normal reply view had the same "deleted parent hides live replies"
  bug** that was fixed for "have I posted yet?" in round 2 — just not in the
  code path most people actually use (reading a discussion thread normally).
  Same fix applied there too.
- **A safety cap on the faster sweeps.** Round 2 made the overdue/discussion
  checks run in parallel per course. On an account with many courses and many
  graded discussions, that could in theory fire off a lot of requests to
  Canvas at once and risk getting temporarily rate-limited. Capped at 8 at a
  time — still much faster than before, without the burst risk.

## Next
1. Committed as `b19ca93` on `main`.
2. Restart/reload the MCP server in whatever client has it configured (Claude
   Desktop, etc.) so it picks up the new code.
3. Run the affected tools against your real Canvas account once and spot-check
   the numbers against the Canvas web UI — everything above was verified with
   simulated API responses across three rounds of code review, not live data.

## Broken
Nothing known to be broken. The one thing fixed along the way but not part of
your original list: the project's `venv/bin/pip` had a stale shebang pointing
at the old folder path (from when `canvas_mcp` was renamed to
`canvas_mcp.nosync`'s parent) and would not run — fixed so `pip install` works
again.

## Needs your decision
- **Speed/cost tradeoff.** The overdue sweep and the discussion sweep each call
  Canvas once per active course, so "what's coming up" and grade estimation are
  now slower and make more requests than before, in exchange for not missing
  anything. My recommendation: keep it — correctness matters more than a few
  extra seconds for a to-do list. Say so if you'd rather it be faster and less
  thorough.
- **pypdf dependency.** Added `pypdf>=4.0.0` to `requirements.txt` and installed
  it into `venv`. This is a new third-party library the server depends on now.
  Low risk (pure Python PDF text extraction, no network access of its own) but
  flagging since it's a new moving part.
