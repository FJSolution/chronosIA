"""
SubgraphCache — Exact content-addressable cache with expiration.
Vector helpers are diagnostics only; approximate results are never reused.
Eliminates redundant LLM calls and re-executions across agent workflows while strictly
guarding entity identifiers (e.g., ORD-9921 vs ORD-8888).
"""

import copy
from threading import RLock
from .locking import synchronized
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional, List, Set, Tuple


# Multilingual intent synonym normalization table for semantic vector projection
_SYNONYM_MAP = {
    "reembolsar": "refund",
    "reembolso": "refund",
    "estornar": "refund",
    "estorno": "refund",
    "refunded": "refund",
    "apagar": "delete",
    "deletar": "delete",
    "remover": "delete",
    "excluir": "delete",
    "drop": "delete",
    "consultar": "select",
    "buscar": "select",
    "verificar": "select",
    "mostrar": "select",
    "listar": "select",
    "pedido": "order",
    "ordem": "order",
    "orders": "order",
    "atualizar": "update",
    "modificar": "update",
    "alterar": "update",
}

_ENTITY_ID_REGEX = re.compile(r"\b(?:ORD-\d+|USR-\d+|INC-\d+|step-\d+|\d+(?:\.\d+)?)\b", re.IGNORECASE)


@dataclass
class CacheEntry:
    subgraph_hash: str
    intent_signature: str
    output_payload: Dict[str, Any]
    tokens_saved_per_hit: int
    cost_saved_per_hit_usd: float
    latency_saved_ms: int
    hits: int = 0
    semantic_hits: int = 0
    created_at: float = 0.0
    match_mode: str = "EXACT_SHA256"
    similarity_score: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SubgraphCache:
    """
    Exact SHA-256 cache for explicitly pure computations, with bounded TTL/storage.
    Vector helpers are retained for diagnostics only, never for cache reuse.
    """

    VECTOR_DIM = 128

    def __init__(self, semantic_threshold: float = 0.86, ttl_seconds: float = 300, max_entries: int = 1024):
        self._lock = RLock()
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._store: Dict[str, CacheEntry] = {}
        self._vectors: Dict[str, Tuple[str, str, Set[str], List[float]]] = {}
        self.semantic_threshold = semantic_threshold
        self.total_hits: int = 0
        self.exact_hits: int = 0
        self.semantic_hits: int = 0
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
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @classmethod
    def _extract_entities(cls, text: str) -> Set[str]:
        """Extracts strict entity IDs and numeric literals so semantic cache never confuses ORD-9921 with ORD-1002."""
        return {m.upper() for m in _ENTITY_ID_REGEX.findall(text)}

    @classmethod
    def compute_semantic_vector(cls, payload: Dict[str, Any]) -> Tuple[Set[str], List[float]]:
        """
        Computes a normalized 128-dimensional feature-hashed embedding vector
        over intent-normalized word tokens and character 3-grams.
        """
        raw_text = json.dumps(payload, sort_keys=True, default=str).lower()
        entities = cls._extract_entities(raw_text)

        tokens = re.findall(r"[a-z0-9_]+", raw_text)
        normalized_tokens = [_SYNONYM_MAP.get(tok, tok) for tok in tokens]

        vec = [0.0] * cls.VECTOR_DIM
        features: List[Tuple[str, float]] = []
        for tok in normalized_tokens:
            features.append((f"w:{tok}", 2.0))
            if len(tok) >= 4:
                for i in range(len(tok) - 2):
                    features.append((f"g:{tok[i:i+3]}", 0.6))

        for feat, weight in features:
            h = int(hashlib.md5(feat.encode("utf-8")).hexdigest()[:8], 16)
            idx = h % cls.VECTOR_DIM
            sign = 1.0 if (h & 1) == 0 else -1.0
            vec[idx] += sign * weight

        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return entities, vec

    @staticmethod
    def cosine_similarity(v1: List[float], v2: List[float]) -> float:
        return sum(a * b for a, b in zip(v1, v2))

    @synchronized
    def lookup(
        self,
        step_type: str,
        name: str,
        input_payload: Dict[str, Any],
        allow_semantic: bool = False,
    ) -> Optional[CacheEntry]:
        if allow_semantic:
            raise ValueError("Approximate cache reuse is disabled; use exact context keys")
        self._expire()
        key = self.compute_hash(step_type, name, input_payload)
        entry = self._store.get(key)
        if entry:
            entry.hits += 1
            entry.match_mode = "EXACT_SHA256"
            entry.similarity_score = 1.0
            self.total_hits += 1
            self.exact_hits += 1
            self.total_tokens_saved += entry.tokens_saved_per_hit
            self.total_cost_saved_usd = round(self.total_cost_saved_usd + entry.cost_saved_per_hit_usd, 6)
            self.total_latency_saved_ms += entry.latency_saved_ms
            return copy.deepcopy(entry)

        self.total_misses += 1
        return None

    @synchronized
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
        self._expire()
        if key not in self._store and len(self._store) >= self.max_entries:
            self._store.pop(next(iter(self._store)))

        entry = CacheEntry(
            subgraph_hash=key,
            intent_signature=f"{step_type}::{name}",
            output_payload=copy.deepcopy(output_payload),
            tokens_saved_per_hit=tokens,
            cost_saved_per_hit_usd=cost_usd,
            latency_saved_ms=latency_ms,
            hits=0,
            semantic_hits=0,
            created_at=time.time(),
            match_mode="EXACT_SHA256",
            similarity_score=1.0,
        )
        self._store[key] = entry
        return copy.deepcopy(entry)

    def _expire(self):
        now = time.time()
        for key in list(self._store):
            if now - self._store[key].created_at >= self.ttl_seconds:
                del self._store[key]

    @synchronized
    def clear(self):
        self._store.clear()
        self._vectors.clear()

    @synchronized
    def stats(self) -> Dict[str, Any]:
        self._expire()
        total_requests = self.total_hits + self.total_misses
        hit_rate = (self.total_hits / total_requests * 100.0) if total_requests > 0 else 0.0
        return {
            "entries_count": len(self._store),
            "total_hits": self.total_hits,
            "exact_hits": self.exact_hits,
            "semantic_hits": self.semantic_hits,
            "total_misses": self.total_misses,
            "hit_rate_pct": round(hit_rate, 1),
            "semantic_threshold": None,
            "match_policy": "EXACT_ONLY",
            "ttl_seconds": self.ttl_seconds,
            "total_tokens_saved": self.total_tokens_saved,
            "total_cost_saved_usd": round(self.total_cost_saved_usd, 4),
            "total_latency_saved_ms": self.total_latency_saved_ms,
            "entries": [e.to_dict() for e in self._store.values()],
        }

