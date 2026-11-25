"""Thin Bedrock helpers: embeddings, re-ranking, and the Claude client."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import boto3
from anthropic import AnthropicBedrock, AsyncAnthropicBedrock
from botocore.config import Config

from . import config


@lru_cache
def runtime():
    return boto3.client("bedrock-runtime", region_name=config.AWS_REGION,
                        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}))


@lru_cache
def claude() -> AnthropicBedrock:
    return AnthropicBedrock(aws_region=config.AWS_REGION)


@lru_cache
def claude_async() -> AsyncAnthropicBedrock:
    return AsyncAnthropicBedrock(aws_region=config.AWS_REGION)


def embed(text: str) -> list[float]:
    body = json.dumps({"inputText": text[:20000], "dimensions": config.EMBED_DIMS, "normalize": True})
    resp = runtime().invoke_model(modelId=config.EMBED_MODEL, body=body)
    return json.loads(resp["body"].read())["embedding"]


def embed_many(texts: list[str], workers: int = 8) -> list[list[float]]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(embed, texts))


def rerank(query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
    """Return (original index, relevance score) for the best `top_n` documents."""
    if not documents:
        return []
    body = json.dumps({"api_version": 2, "query": query, "documents": documents,
                       "top_n": min(top_n, len(documents))})
    resp = runtime().invoke_model(modelId=config.RERANK_MODEL, body=body)
    return [(r["index"], r["relevance_score"]) for r in json.loads(resp["body"].read())["results"]]
