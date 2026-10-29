"""
SubgraphCache — Hybrid Content-Addressable (SHA-256) & Semantic Vector Cosine Cache.
Eliminates redundant LLM calls and re-executions across agent workflows while strictly
guarding entity identifiers (e.g., ORD-9921 vs ORD-8888).
"""

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
    Caches deterministic agent sub-routines and validated LLM/tool subgraphs
    using both exact SHA-256 content addressing and 128-dim Semantic Cosine Similarity
    with strict entity ID verification.
    """

    VECTOR_DIM = 128

    def __init__(self, semantic_threshold: float = 0.86):
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
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

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

    def lookup(
        self,
        step_type: str,
        name: str,
        input_payload: Dict[str, Any],
        allow_semantic: bool = True,
    ) -> Optional[CacheEntry]:
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
            return entry

        # Fallback: Semantic Vector Cosine Lookup (with strict Entity ID guard)
        if allow_semantic and self._vectors:
            query_entities, query_vec = self.compute_semantic_vector(input_payload)
            best_key: Optional[str] = None
            best_score: float = 0.0

            for cand_key, (cand_type, cand_name, cand_entities, cand_vec) in self._vectors.items():
                if cand_type != step_type or cand_name != name:
                    continue
                # Strict entity ID guard: never match if order IDs or numbers differ
                if cand_entities != query_entities:
                    continue
                sim = self.cosine_similarity(query_vec, cand_vec)
                if sim >= self.semantic_threshold and sim > best_score:
                    best_score = sim
                    best_key = cand_key

            if best_key and best_key in self._store:
                sem_entry = self._store[best_key]
                sem_entry.hits += 1
                sem_entry.semantic_hits += 1
                sem_entry.match_mode = "SEMANTIC_VECTOR"
                sem_entry.similarity_score = round(best_score, 4)
                self.total_hits += 1
                self.semantic_hits += 1
                self.total_tokens_saved += sem_entry.tokens_saved_per_hit
                self.total_cost_saved_usd = round(self.total_cost_saved_usd + sem_entry.cost_saved_per_hit_usd, 6)
                self.total_latency_saved_ms += sem_entry.latency_saved_ms
                return sem_entry

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
        entities, vec = self.compute_semantic_vector(input_payload)
        self._vectors[key] = (step_type, name, entities, vec)

        entry = CacheEntry(
            subgraph_hash=key,
            intent_signature=f"{step_type}::{name}",
            output_payload=output_payload,
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
        return entry

    def stats(self) -> Dict[str, Any]:
        total_requests = self.total_hits + self.total_misses
        hit_rate = (self.total_hits / total_requests * 100.0) if total_requests > 0 else 0.0
        return {
            "entries_count": len(self._store),
            "total_hits": self.total_hits,
            "exact_hits": self.exact_hits,
            "semantic_hits": self.semantic_hits,
            "total_misses": self.total_misses,
            "hit_rate_pct": round(hit_rate, 1),
            "semantic_threshold": self.semantic_threshold,
            "total_tokens_saved": self.total_tokens_saved,
            "total_cost_saved_usd": round(self.total_cost_saved_usd, 4),
            "total_latency_saved_ms": self.total_latency_saved_ms,
            "entries": [e.to_dict() for e in self._store.values()],
        }

