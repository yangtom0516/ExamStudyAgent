"""Keyword (sparse) vectors for the exact-word half of hybrid search.

We count the words of each chunk (BM25-style); Qdrant adds the "rare words count more" part
itself (the collection's sparse vector uses `modifier=IDF`). See CLAUDE.md, Knowledge base.

A sparse vector is a list of (word id, weight) pairs for only the words the text contains.
Word ids come from a stable hash of the word, so no vocabulary has to be stored anywhere.
"""

import hashlib
import re
from collections import Counter

from qdrant_client import models

# Version numbers like "3.2" stay one token; otherwise runs of letters/digits/underscores
# (so get_view stays whole), keeping a trailing + or # (C++, C#). Hyphens split ("split-brain").
_TOKEN = re.compile(r"\d+(?:\.\d+)+|\w+[+#]*")

# BM25's two knobs: repeats help less and less (K1), and long chunks don't win just by being
# long (B, measured against a typical chunk length).
K1 = 1.2
B = 0.75
AVERAGE_TOKENS = 120  # roughly an 800-character chunk


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def token_id(token: str) -> int:
    """Stable 32-bit id for a word (Qdrant sparse indices are unsigned 32-bit integers)."""
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), "big")


def _to_sparse(weights: dict[int, float]) -> models.SparseVector:
    indices = sorted(weights)
    return models.SparseVector(indices=indices, values=[weights[i] for i in indices])


def document_vector(text: str) -> models.SparseVector:
    """Keyword vector of a chunk: each word's BM25 term weight."""
    tokens = tokenize(text)
    length_factor = 1 - B + B * len(tokens) / AVERAGE_TOKENS
    weights: dict[int, float] = {}
    for token, count in Counter(tokens).items():
        index = token_id(token)
        # (Two different words sharing an id is astronomically unlikely; their weights would add.)
        weights[index] = weights.get(index, 0.0) + count * (K1 + 1) / (count + K1 * length_factor)
    return _to_sparse(weights)


def query_vector(text: str) -> models.SparseVector:
    """Keyword vector of a search query: each distinct word once, weight 1."""
    return _to_sparse({token_id(token): 1.0 for token in set(tokenize(text))})
