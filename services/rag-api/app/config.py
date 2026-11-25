"""Runtime settings, all from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://rag:rag@localhost:5433/rag")
CORPUS_ROOT = Path(os.getenv("CORPUS_ROOT", Path(__file__).resolve().parents[3]))
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "dev-key")

# Bedrock identifiers, confirmed with live calls on 2026-11-24 (see PLAN.md, Phase 0).
EMBED_MODEL = os.getenv("EMBED_MODEL", "amazon.titan-embed-text-v2:0")
EMBED_DIMS = 1024
RERANK_MODEL = os.getenv("RERANK_MODEL", "cohere.rerank-v3-5:0")
ROUTER_MODEL = os.getenv("ROUTER_MODEL", "us.anthropic.claude-haiku-5-5")

ANSWER_MODELS = {
    "opus": os.getenv("OPUS_MODEL", "us.anthropic.claude-opus-5-5"),
    "fable": os.getenv("FABLE_MODEL", "us.anthropic.claude-fable-5-2"),
}
ANSWER_EFFORT = os.getenv("ANSWER_EFFORT", "medium")
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "6000"))

# Retrieval knobs.
CANDIDATES_PER_SEARCH = 20     # per jurisdiction, per search type, per query
RERANK_POOL = 40               # merged candidates sent to the re-ranker
TOP_K_LOCAL = 8                # passages kept per local jurisdiction
TOP_K_STATE = 3                # passages kept from statewide guidance
TOP_K_NAIVE = 10               # single pooled search, for the comparison mode
RRF_K = 60
SECTION_EXPANSION = 4          # extra sibling passages pulled in per jurisdiction

# Spend guard: total tokens (input + output) allowed per UTC day across all users.
DAILY_TOKEN_LIMIT = int(os.getenv("DAILY_TOKEN_LIMIT", "3000000"))

# Request size caps, so one oversized conversation cannot run past the daily limit.
MAX_HISTORY_MESSAGES = 20          # most recent turns sent to the model
MAX_MESSAGE_CHARS = 12000          # per message (~3k tokens)
MAX_REQUEST_MESSAGES = 200         # anything larger is rejected outright
RETRIEVAL_TOKEN_ALLOWANCE = 20000  # rough upper bound for retrieved passages + prompts
