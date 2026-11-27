"""Rotate a dedicated Canvas MCP token every 20 days."""

import argparse
import getpass
import json
import os
import sys
import tempfile
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx


TOKEN_LIST = "/api/v1/users/self/user_generated_tokens"
TOKENS = "/api/v1/users/self/tokens"
PROFILE = "/api/v1/users/self/profile"
ROTATION_INTERVAL = timedelta(days=20)


def canvas_origin(raw):
    url = urlsplit(raw)
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.path not in ("", "/") or url.query or url.fragment):
        raise ValueError("Use the HTTPS origin of your Canvas site.")
    return str(httpx.URL(raw).copy_with(path="/")).rstrip("/")


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_state(path):
    with path.open() as file:
        metadata = os.fstat(file.fileno())
        if metadata.st_mode & 0o077 or (hasattr(os, "getuid") and metadata.st_uid != os.getuid()):
            raise ValueError("The Canvas token file must be private.")
        return json.load(file)


def save(path, state):
    with tempfile.NamedTemporaryFile(mode="w", prefix=".canvas-token-", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(state, file)
            file.flush()
            os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def request(client, method, url, token, **kwargs):
    headers = {"Authorization": f"Bearer {token}"}
    if "content" in kwargs:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    response = client.request(method, url, headers=headers, **kwargs)
    if method == "DELETE" and response.status_code == 404:
        return None
    response.raise_for_status()
    if method == "DELETE":
        return None
    return response.json()


def token_records(client, token):
    records = []
    visited = set()
    endpoint = client.base_url.join(TOKEN_LIST)
    url = str(endpoint.copy_with(query=b"per_page=100"))
    while url:
        if httpx.URL(url).copy_with(query=None) != endpoint or url in visited:
            raise ValueError("Unexpected token list URL")
        visited.add(url)
        response = client.get(url, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        records.extend(response.json())
        url = response.links.get("next", {}).get("url")
    return records


def token_record(records, token_id):
    matches = [record for record in records if record["id"] == token_id]
    if len(matches) != 1 or not matches[0].get("expires_at"):
        raise ValueError("Cannot identify the current token and expiration")
    return matches[0]


def rotate(client, path, now, *, check_only=False, rotate_now=False):
    state = read_state(path)
    if canvas_origin(state["base_url"]) != canvas_origin(str(client.base_url)):
        raise ValueError("The Canvas origin does not match the token file.")
    next_rotation = state.get("next_rotation")
    if not (check_only or rotate_now) and next_rotation and now < timestamp(next_rotation):
        return
    token = state["token"]
    records = token_records(client, token)
    record = token_record(records, state["id"])
    expires = timestamp(record["expires_at"])
    print(f"Current token expiration: {expires.isoformat()}", flush=True)
    previous = state.get("previous")
    if check_only:
        return
    if previous:
        if rotate_now and now < timestamp(previous["expires_at"]):
            raise RuntimeError("The previous Canvas token has not expired. The script kept both tokens.")
        if previous["id"] == record["id"]:
            raise ValueError("Previous token matches the current token")
        request(client, "GET", PROFILE, token)
        request(client, "DELETE", f"{TOKENS}/{int(previous['id'])}", token)
        state.pop("previous")
        save(path, state)
        print("Removed the previous token.", flush=True)
    purpose = f"Canvas MCP replacement for token {int(record['id'])}"
    if any(item.get("purpose") == purpose for item in records):
        raise RuntimeError("A replacement already exists in Canvas. Check Approved Integrations before retrying.")
    profile = request(client, "GET", PROFILE, token)
    new_expiration = now + timedelta(days=30, minutes=-1)
    replacement = request(
        client, "POST", TOKENS, token,
        content=urlencode([
            ("token[purpose]", purpose),
            ("token[expires_at]", new_expiration.isoformat()),
            *(("token[scopes][]", scope) for scope in record["scopes"]),
        ]),
    )
    replacement_id = int(replacement["id"])
    if any(item["id"] == replacement_id for item in records):
        raise ValueError("Canvas returned an existing token ID")
    try:
        new_token = replacement.get("token") or replacement["visible_token"]
        if new_token.endswith("..."):
            raise ValueError("Canvas returned a token hint instead of a token")
        new_profile = request(client, "GET", PROFILE, new_token)
        if new_profile["id"] != profile["id"]:
            raise ValueError("Replacement does not match the expected Canvas account")
        new_record = token_record(token_records(client, new_token), replacement_id)
        if set(new_record["scopes"]) != set(record["scopes"]):
            raise ValueError("Replacement does not match the expected Canvas permissions")
        new_due = min(now + ROTATION_INTERVAL, timestamp(new_record["expires_at"]) - timedelta(days=7))
        if new_due <= now:
            raise ValueError("Replacement expires too soon")
        save(path, {
            "base_url": state["base_url"], "token": new_token, "id": replacement_id,
            "next_rotation": new_due.isoformat(),
            "previous": {"id": record["id"], "expires_at": record["expires_at"]},
        })
    except Exception:
        try:
            request(client, "DELETE", f"{TOKENS}/{replacement_id}", token)
        except httpx.HTTPError:
            print("Could not remove the unverified replacement.", flush=True)
        raise
    print("Installed and verified the new token. Kept the old token for later removal.", flush=True)


def initialize(client, path, token, now):
    matches = []
    for record in token_records(client, token):
        hint = record.get("visible_token") or ""
        if hint.endswith("...") and hint[:-3] and token.startswith(hint[:-3]):
            matches.append(record)
    if len(matches) != 1 or not matches[0].get("expires_at"):
        raise RuntimeError("Canvas did not identify a unique token with an expiration date.")
    record = matches[0]
    request(client, "GET", PROFILE, token)
    next_rotation = min(now + ROTATION_INTERVAL, timestamp(record["expires_at"]) - timedelta(days=7))
    save(path, {
        "base_url": canvas_origin(str(client.base_url)), "token": token,
        "id": record["id"], "next_rotation": next_rotation.isoformat(),
    })
    print("Verified and saved the Canvas token.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("token_file", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", metavar="CANVAS_HTTPS_ORIGIN", help="Save a dedicated token from a hidden prompt.")
    mode.add_argument("--check-only", action="store_true")
    mode.add_argument("--rotate-now", action="store_true")
    args = parser.parse_args()
    try:
        import fcntl
    except ImportError:
        parser.exit(1, "Token rotation requires macOS or Linux.\n")
    path = args.token_file.expanduser()
    try:
        if path.is_symlink() or path.parent.stat().st_mode & 0o077:
            raise ValueError("Use a token file in a private directory.")
        with path.with_name(f".{path.name}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            now = datetime.now(timezone.utc)
            if args.init and (path.exists() or not sys.stdin.isatty()):
                raise RuntimeError("Initialization requires a new token file and an interactive terminal.")
            origin = canvas_origin(args.init or read_state(path)["base_url"])
            trust_env = os.environ.get("CANVAS_TRUST_ENV", "0").lower() in {"1", "true", "yes"}
            with httpx.Client(base_url=origin, timeout=15, trust_env=trust_env) as client:
                if args.init:
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", getpass.GetPassWarning)
                        token = getpass.getpass(f"Dedicated API token for {origin}: ").strip()
                    if not token:
                        raise RuntimeError("A dedicated token is required.")
                    initialize(client, path, token, now)
                else:
                    rotate(client, path, now, check_only=args.check_only, rotate_now=args.rotate_now)
    except httpx.HTTPStatusError as error:
        parser.exit(1, f"Canvas returned HTTP {error.response.status_code}.\n")
    except (EOFError, getpass.GetPassWarning):
        parser.exit(1, "Could not read the token from a hidden terminal prompt.\n")
    except RuntimeError as error:
        parser.exit(1, f"{error}\n")
    except (OSError, httpx.HTTPError, ValueError, TypeError, KeyError):
        parser.exit(1, "Canvas token rotation failed. Check the token file and account permissions.\n")


if __name__ == "__main__":
    main()
