# STATE

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
