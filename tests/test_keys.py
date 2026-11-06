"""API-key cleaning and diagnosis (stdlib only, no network, never reads the real config)."""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("WOWAM_CONFIG_DIR", tempfile.mkdtemp())  # importing wowam creates its config dir
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wowam

problems = []


def check(name, cond):
    if not cond:
        problems.append(name)


GOOD = "$2a$10$" + "AbCdEfGhIj" * 5 + "KlM"      # made up: 60 chars, the usual shape
check("fixture has the usual shape", len(GOOD) == 60)

# --- cleaning
check("clean: surrounding spaces and newline", wowam.clean_api_key(f"  {GOOD}\r\n") == GOOD)
check("clean: straight quotes", wowam.clean_api_key(f"'{GOOD}'") == GOOD and wowam.clean_api_key(f'"{GOOD}"') == GOOD)
check("clean: curly quotes", wowam.clean_api_key(f"“{GOOD}”") == GOOD)
check("clean: zero-width and BOM characters", wowam.clean_api_key(f"﻿{GOOD[:10]}​{GOOD[10:]} ") == GOOD)
check("clean: an ordinary key is untouched", wowam.clean_api_key(GOOD) == GOOD)
check("clean: empty and None", wowam.clean_api_key("") == "" and wowam.clean_api_key(None) == "")

# --- diagnosis (None means: the key looks normal, so the cause is something else)
check("a normal key has no shape problem", wowam.key_problem(GOOD) is None)
check("empty key", "No key" in wowam.key_problem(""))
pat = wowam.key_problem("cfc_pat_" + "0123456789abcdef" * 4)
check("personal access token is named as such", "personal access token" in pat and "$2a$10$" in pat)
check("UUID is flagged as an upload token", "upload token" in wowam.key_problem("123e4567-e89b-12d3-a456-426614174000"))
check("inner whitespace", "space" in wowam.key_problem(GOOD[:20] + " " + GOOD[20:]))
short = wowam.key_problem("$2a$10$abc")
check("truncated/mangled key reports its length and the fix", "10 characters" in short and "single quotes" in short
      and "--api-key-prompt" in short)
check("a long key is also flagged", "61 characters" in wowam.key_problem(GOOD + "x"))
check("what a shell leaves behind (2 chars) is flagged", wowam.key_problem("a0") is not None)
check("random text is flagged", "doesn't look like" in wowam.key_problem("hunter2hunter2"))

print("FAIL: " + "; ".join(problems) if problems else "ok")
sys.exit(1 if problems else 0)
