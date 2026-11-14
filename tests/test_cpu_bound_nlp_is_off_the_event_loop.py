"""CPU-bound NLP inside `SearchService.semantic_search` must not run on the loop.

`semantic_search` is `async def`, but both of the functions it calls are plain
synchronous CPU work:

  * ``semantic_search_knowledge`` lazily ``import spacy`` (4.3s measured cold)
    and ``spacy.load("en_core_web_sm")`` (1.0s) on first use, then runs
    ``nlp(text)`` over every source, fits a scikit-learn ``TfidfVectorizer`` and
    computes cosine similarity;
  * ``enhance_query_with_entities`` post-processes that NER output.

Called bare from an `async def`, all of that executes on the event-loop thread
and freezes the entire process — including `/health/ready`. This service runs
ONE replica and the readiness probe is `timeoutSeconds: 5, failureThreshold: 3`,
so roughly 30s of blocking is enough to drop the pod out of the Service's
endpoints, with no second replica to absorb the traffic. The fix is
``await asyncio.to_thread(...)`` around both call sites.

These tests are BEHAVIOURAL, not a source grep: they stub each function with a
synchronous callable that records ``threading.get_ident()`` and compare it to the
ident of the thread running the loop. A reversion to a bare call makes the two
idents equal and fails here. The stubs also mean the real spaCy/sklearn stack is
never loaded, so this file stays fast and deterministic.

Timing margins below are deliberately wide (a 1.0s block against a 0.05s
concurrent tick). This box runs heavily loaded and tight timing margins have
produced false failures before (issue #106) — the property being pinned is
"the loop kept turning at all", which does not need precision.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time

import pytest

import services.search_service  # noqa: F401  (registers it in sys.modules)
from schemas.common import SemanticSearchResponse
from services.search_service import SearchService

# ⚠️ Fetch the module out of sys.modules — NOT `services.search_service.X` as a
# monkeypatch string, and NOT `import services.search_service as m`.
#
# `services/__init__.py:270` does `search_service = SearchService()`, binding an
# INSTANCE over the submodule's name inside the `services` package namespace.
# Both the dotted-string form and `import ... as` go through
# `getattr(services, "search_service")`, so both hand back that instance and
# fail with "'SearchService' object has no attribute
# 'semantic_search_knowledge'". `sys.modules` is keyed by the real dotted path
# and the shadowing cannot intercept it.
_search_service_module = sys.modules["services.search_service"]

# asyncio_mode=auto (pyproject) runs async tests without an explicit mark.

# What the stubbed `semantic_search_knowledge` returns: (results, entities).
_STUB_RESULTS = [
    {
        "topic": "architecture",
        "title": "Stubbed Source",
        "url": "https://example.invalid/stub",
        "note": "not the real knowledge base",
        "similarity_score": 0.75,
    }
]
_STUB_ENTITIES = [{"text": "TOGAF", "label": "ORG", "description": "a framework"}]
_STUB_ENHANCED = "stub enhanced query"

# Long enough that a bare call is unmistakably visible on the loop, short enough
# to keep the suite quick.
_BLOCK_S = 1.0
_TICK_S = 0.05


def _patch_knowledge(monkeypatch, record: dict, *, block: float = 0.0):
    """Stub `semantic_search_knowledge` where `search_service` looks it up.

    Deliberately a *synchronous* def — that is what production calls, and it is
    what makes "did this run on the loop thread?" a meaningful question.
    """

    def fake(*, query, sources, k, min_similarity):
        record["knowledge_ident"] = threading.get_ident()
        record["knowledge_kwargs"] = {
            "query": query,
            "sources": sources,
            "k": k,
            "min_similarity": min_similarity,
        }
        if block:
            time.sleep(block)
        record["knowledge_done_at"] = time.monotonic()
        return _STUB_RESULTS, _STUB_ENTITIES

    monkeypatch.setattr(_search_service_module, "semantic_search_knowledge", fake)


def _patch_enhance(monkeypatch, record: dict, *, block: float = 0.0):
    """Stub `enhance_query_with_entities` at the same lookup point."""

    def fake(query, found_entities):
        record["enhance_ident"] = threading.get_ident()
        record["enhance_args"] = (query, found_entities)
        if block:
            time.sleep(block)
        record["enhance_done_at"] = time.monotonic()
        return _STUB_ENHANCED

    monkeypatch.setattr(_search_service_module, "enhance_query_with_entities", fake)


@pytest.fixture
def record() -> dict:
    return {}


# ---------------------------------------------------------------------------
# 1. THE DEFECT: the TF-IDF + NER call must execute off the loop thread.
# ---------------------------------------------------------------------------
async def test_semantic_search_knowledge_runs_on_a_worker_thread(monkeypatch, record):
    """Pins that `semantic_search_knowledge` is offloaded, not called bare."""
    loop_ident = threading.get_ident()
    _patch_knowledge(monkeypatch, record)
    _patch_enhance(monkeypatch, record)

    await SearchService().semantic_search(query="togaf views", k=3, min_similarity=0.1)

    assert record.get("knowledge_ident") is not None, "the stub was never called"
    assert record["knowledge_ident"] != loop_ident, (
        "semantic_search_knowledge ran on the event-loop thread — spaCy load + "
        "TF-IDF fit now block every other request, including /health/ready"
    )


# ---------------------------------------------------------------------------
# 2. The second call site is just as blocking and must be offloaded too.
# ---------------------------------------------------------------------------
async def test_enhance_query_with_entities_runs_on_a_worker_thread(monkeypatch, record):
    """Pins that the SECOND sync call site is offloaded, not only the first."""
    loop_ident = threading.get_ident()
    _patch_knowledge(monkeypatch, record)
    _patch_enhance(monkeypatch, record)

    await SearchService().semantic_search(query="togaf views", k=3, min_similarity=0.1)

    assert record.get("enhance_ident") is not None, "the stub was never called"
    assert record["enhance_ident"] != loop_ident, (
        "enhance_query_with_entities ran on the event-loop thread"
    )


# ---------------------------------------------------------------------------
# 3. The point of the offload: the loop keeps serving while NLP is running.
# ---------------------------------------------------------------------------
async def test_event_loop_stays_responsive_during_the_knowledge_call(monkeypatch, record):
    """Pins loop responsiveness — a concurrent task must not wait out the CPU work."""
    _patch_knowledge(monkeypatch, record, block=_BLOCK_S)
    _patch_enhance(monkeypatch, record)

    async def ticker() -> float:
        await asyncio.sleep(_TICK_S)
        return time.monotonic()

    tick = asyncio.create_task(ticker())
    await SearchService().semantic_search(query="togaf views", k=3, min_similarity=0.1)
    tick_at = await tick

    assert "knowledge_done_at" in record, "the blocking stub was never called"
    assert tick_at < record["knowledge_done_at"], (
        f"a {_TICK_S}s task only completed AFTER {_BLOCK_S}s of CPU work finished — "
        "the event loop was blocked for the whole duration"
    )


async def test_event_loop_stays_responsive_during_the_enhance_call(monkeypatch, record):
    """Same responsiveness property for the second offloaded call site."""
    _patch_knowledge(monkeypatch, record)
    _patch_enhance(monkeypatch, record, block=_BLOCK_S)

    async def ticker() -> float:
        await asyncio.sleep(_TICK_S)
        return time.monotonic()

    tick = asyncio.create_task(ticker())
    await SearchService().semantic_search(query="togaf views", k=3, min_similarity=0.1)
    tick_at = await tick

    assert "enhance_done_at" in record, "the blocking stub was never called"
    assert tick_at < record["enhance_done_at"], (
        f"a {_TICK_S}s task only completed AFTER {_BLOCK_S}s of CPU work finished — "
        "the event loop was blocked for the whole duration"
    )


# ---------------------------------------------------------------------------
# 4. Offloading must not change what the endpoint returns.
# ---------------------------------------------------------------------------
async def test_offload_preserves_the_response_payload(monkeypatch, record):
    """Pins that to_thread is transparent: results/entities/enhanced_query survive."""
    _patch_knowledge(monkeypatch, record)
    _patch_enhance(monkeypatch, record)

    response = await SearchService().semantic_search(query="togaf views", k=3, min_similarity=0.1)

    assert isinstance(response, SemanticSearchResponse)
    assert response.query == "togaf views"
    assert response.enhanced_query == _STUB_ENHANCED
    assert response.total_results == len(_STUB_RESULTS) == 1
    assert [(r.topic, r.title, r.url, r.similarity_score) for r in response.results] == [
        ("architecture", "Stubbed Source", "https://example.invalid/stub", 0.75)
    ]
    assert [(e.text, e.label, e.description) for e in response.entities] == [
        ("TOGAF", "ORG", "a framework")
    ]


# ---------------------------------------------------------------------------
# 5. The arguments must still arrive intact across the thread boundary.
# ---------------------------------------------------------------------------
async def test_offloaded_calls_receive_the_original_arguments(monkeypatch, record):
    """Pins that to_thread forwards query/k/min_similarity/SOURCES unchanged."""
    from seed_knowledge import SOURCES

    _patch_knowledge(monkeypatch, record)
    _patch_enhance(monkeypatch, record)

    await SearchService().semantic_search(query="critical chain", k=7, min_similarity=0.42)

    kwargs = record.get("knowledge_kwargs")
    assert kwargs is not None, "the stub was never called"
    assert kwargs["query"] == "critical chain"
    assert kwargs["k"] == 7
    assert kwargs["min_similarity"] == 0.42
    assert kwargs["sources"] is SOURCES

    assert record.get("enhance_args") == ("critical chain", _STUB_ENTITIES)


# ---------------------------------------------------------------------------
# 6. The no-query short circuit must not touch the NLP stack at all.
# ---------------------------------------------------------------------------
async def test_empty_query_never_reaches_the_cpu_bound_path(monkeypatch, record):
    """Pins the cheap short-circuit: no query means no spaCy, no TF-IDF, no thread."""
    _patch_knowledge(monkeypatch, record)
    _patch_enhance(monkeypatch, record)

    response = await SearchService().semantic_search(query=None, k=3, min_similarity=0.1)

    assert response.entities == []
    assert "knowledge_ident" not in record
    assert "enhance_ident" not in record
