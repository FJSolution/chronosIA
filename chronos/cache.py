"""
SubgraphCache — Content-Addressable & Intent-Normalized Semantic Subgraph Cache.
Eliminates redundant LLM calls and re-executions across agent workflows.
"""

import hashlib
import json
import time
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional


@dataclass
class CacheEntry:
    subgraph_hash: str
    intent_signature: str
    output_payload: Dict[str, Any]
    tokens_saved_per_hit: int
    cost_saved_per_hit_usd: float
    latency_saved_ms: int
    hits: int = 0
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SubgraphCache:
    """
    Caches deterministic agent sub-routines and validated LLM/tool subgraphs.
    """

    def __init__(self):
        self._store: Dict[str, CacheEntry] = {}
        self.total_hits: int = 0
        self.total_misses: int = 0
        self.total_tokens_saved: int = 0
        self.total_cost_saved_usd: float = 0.0
        self.total_latency_saved_ms: int = 0

    @staticmethod
    def compute_hash(step_type: str, name: str, input_payload: Dict[str, Any]) -> str:
        normalized = json.dumps(
            {"type": step_type, "name": name, "input": input_payload},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def lookup(self, step_type: str, name: str, input_payload: Dict[str, Any]) -> Optional[CacheEntry]:
        key = self.compute_hash(step_type, name, input_payload)
        entry = self._store.get(key)
        if entry:
            entry.hits += 1
            self.total_hits += 1
            self.total_tokens_saved += entry.tokens_saved_per_hit
            self.total_cost_saved_usd = round(self.total_cost_saved_usd + entry.cost_saved_per_hit_usd, 6)
            self.total_latency_saved_ms += entry.latency_saved_ms
            return entry
        self.total_misses += 1
        return None

    def put(
        self,
        step_type: str,
        name: str,
        input_payload: Dict[str, Any],
        output_payload: Dict[str, Any],
        tokens: int,
        cost_usd: float,
        latency_ms: int,
    ) -> CacheEntry:
        key = self.compute_hash(step_type, name, input_payload)
        entry = CacheEntry(
            subgraph_hash=key,
            intent_signature=f"{step_type}::{name}",
            output_payload=output_payload,
            tokens_saved_per_hit=tokens,
            cost_saved_per_hit_usd=cost_usd,
            latency_saved_ms=latency_ms,
            hits=0,
            created_at=time.time(),
        )
        self._store[key] = entry
        return entry

    def stats(self) -> Dict[str, Any]:
        total_requests = self.total_hits + self.total_misses
        hit_rate = (self.total_hits / total_requests * 100.0) if total_requests > 0 else 0.0
        return {
            "entries_count": len(self._store),
            "total_hits": self.total_hits,
            "total_misses": self.total_misses,
            "hit_rate_pct": round(hit_rate, 1),
            "total_tokens_saved": self.total_tokens_saved,
            "total_cost_saved_usd": round(self.total_cost_saved_usd, 4),
            "total_latency_saved_ms": self.total_latency_saved_ms,
            "entries": [e.to_dict() for e in self._store.values()],
        }
