"""Shared regexes for repo names, commit ids and related site answers."""

import re

# author/model as typed or read from a line of text (looser form).
REPO = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
REPO_LINE = re.compile(r"^(" + REPO.pattern + r")(?:\s|$)")

# author/model parsed out of the site's own answers (stricter form: alnum start, 1-96 chars per part).
REPO_STRICT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}")

# 40-character commit ids: lowercase-only (index mirror) and any case (submissions).
COMMIT_LOWER = re.compile(r"[0-9a-f]{40}")
COMMIT_ANY = re.compile(r"[0-9a-fA-F]{40}")
