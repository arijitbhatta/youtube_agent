"""Config-over-code seam (CLAUDE.md principle 5): model tiering and the
cache path live in `.env`, not hardcoded per call site. Loaded once here;
every other module imports these constants instead of reading os.environ
directly, so there's exactly one place that reads the .env file.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

# Calls go through OpenRouter (OpenAI-Chat-Completions-compatible), which
# proxies to Anthropic under an "anthropic/..." model slug -- see
# agent/llm_client.py. Swappable to a different provider/model here only.
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
MODEL_CHEAP = os.environ.get("MODEL_CHEAP", "anthropic/claude-haiku-4.5")
MODEL_STRONG = os.environ.get("MODEL_STRONG", "anthropic/claude-sonnet-5")
CACHE_PATH = REPO_ROOT / os.environ.get("CACHE_PATH", "outputs/cache.sqlite3")
OUTPUTS_DIR = REPO_ROOT / "outputs"

# HLD §6.6 ablation flag -- the default for a run that doesn't say
# otherwise. run.py's --no-reviewer and ui/app.py's toggle override this
# per run; eval/run_eval.py's --compare-reviewer mode (Phase 8) runs both.
ENABLE_REVIEWER_DEFAULT = os.environ.get("ENABLE_REVIEWER_DEFAULT", "true").lower() != "false"

# Bounded concurrency for agent/graph.py's fetch stage (SKILLS.md #13):
# yt-dlp's extract_info + the caption-CDN request are both I/O-bound, so a
# thread pool gives a real wall-clock win on ~80 sequential candidates --
# but transcripts.py already documents that CDN 429-ing under sustained
# volume (an IP-level throttle, not per-video), so this stays a small
# explicit bound rather than "one thread per candidate."
FETCH_MAX_WORKERS = int(os.environ.get("FETCH_MAX_WORKERS", "8"))


def api_key_present() -> bool:
    """Whether OPENROUTER_API_KEY is set. Checked before any live call site;
    the value itself is never read back or logged from here."""
    return bool(os.environ.get("OPENROUTER_API_KEY"))
