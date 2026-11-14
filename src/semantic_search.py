"""
Lightweight semantic search with NER capabilities.

This module provides enhanced search functionality including:
- Named Entity Recognition for extracting entities from queries
- TF-IDF based semantic similarity scoring
- Entity-aware search across knowledge base
"""

import re
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from spacy import Language

# Global variables for lazy loading
_nlp: "Language" | None = None
_vectorizer: Any | None = None
_knowledge_vectors: Any | None = None
logger = structlog.get_logger(__name__)

# Custom business/methodology terms for entity recognition
CUSTOM_ENTITIES = {
    "RICE": {
        "label": "METHODOLOGY",
        "description": "Reach, Impact, Confidence, Effort prioritization framework",
        "patterns": [
            r"\bRICE\b",
            r"\bRICE methodology\b",
            r"\beach, impact, confidence, effort\b",
        ],
    },
    "WSJF": {
        "label": "METHODOLOGY",
        "description": "Weighted Shortest Job First prioritization method",
        "patterns": [r"\bWSJF\b", r"\bweighted shortest job first\b"],
    },
    "SAFe": {
        "label": "FRAMEWORK",
        "description": "Scaled Agile Framework",
        "patterns": [r"\bSAFe\b", r"\bscaled agile framework\b"],
    },
    "ISO/IEC/IEEE 29148": {
        "label": "STANDARD",
        "description": "Requirements Engineering standard",
        "patterns": [r"\bISO[/\s]*IEC[/\s]*IEEE\s*29148\b", r"\b29148\b"],
    },
    "Requirements Engineering": {
        "label": "DISCIPLINE",
        "description": "Software requirements engineering practice",
        "patterns": [r"\brequirements? engineering\b"],
    },
}


def get_nlp() -> "Language":
    """Get or load the spaCy model."""
    global _nlp
    if _nlp is None:
        import spacy

        try:
            _nlp = spacy.load("en_core_web_sm")
        except OSError:
            # Fallback to basic tokenization if model not available
            _nlp = spacy.blank("en")
    return _nlp


def extract_custom_entities(text: str) -> list[dict[str, Any]]:
    """Extract custom business/methodology entities using pattern matching."""
    entities: list[dict[str, Any]] = []
    seen_entities: set[tuple[str, str]] = set()  # Avoid duplicates

    for entity_name, entity_info in CUSTOM_ENTITIES.items():
        label = str(entity_info["label"])
        for pattern in entity_info["patterns"]:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for _ in matches:
                # Normalize the matched text to the canonical entity name
                entity_key = (entity_name, label)

                if entity_key not in seen_entities:
                    entities.append(
                        {
                            "text": entity_name,  # Use canonical name
                            "label": label,
                            "description": entity_info["description"],
                        }
                    )
                    seen_entities.add(entity_key)

    return entities


def extract_entities(text: str) -> list[dict[str, str]]:
    """Extract named entities from text using spaCy NER + custom patterns."""
    nlp = get_nlp()
    doc = nlp(text)

    entities = []
    seen_texts = set()  # Avoid duplicates

    # First, get standard spaCy entities
    import spacy as _spacy

    for ent in doc.ents:
        entity_key = (ent.text.lower(), ent.label_)
        if entity_key not in seen_texts:
            entities.append(
                {
                    "text": ent.text,
                    "label": ent.label_,
                    "description": _spacy.explain(ent.label_) or ent.label_,
                }
            )
            seen_texts.add(entity_key)

    # Then, add custom business/methodology entities
    custom_entities = extract_custom_entities(text)
    for custom_ent in custom_entities:
        entity_key = (custom_ent["text"].lower(), custom_ent["label"])
        if entity_key not in seen_texts:
            entities.append(custom_ent)
            seen_texts.add(entity_key)

    return entities


def preprocess_text(text: str) -> str:
    """Preprocess text for better matching."""
    # Convert to lowercase and normalize whitespace
    text = re.sub(r"\s+", " ", text.lower().strip())

    # Extract key terms using simple patterns
    nlp = get_nlp()
    doc = nlp(text)

    # Keep important tokens (not stop words, punctuation)
    important_tokens = [
        token.lemma_ for token in doc if not token.is_stop and not token.is_punct and token.is_alpha
    ]

    return " ".join(important_tokens) if important_tokens else text


def build_knowledge_index(sources: list[dict[str, Any]]) -> None:
    """Build TF-IDF index for knowledge sources."""
    global _vectorizer, _knowledge_vectors

    if _vectorizer is None:
        from sklearn.feature_extraction.text import TfidfVectorizer

        # Prepare documents for vectorization
        documents = []
        for source in sources:
            # Combine all searchable fields
            doc_text = f"{source['topic']} {source['title']} {source['note']}"
            documents.append(preprocess_text(doc_text))

        # Create TF-IDF vectorizer
        _vectorizer = TfidfVectorizer(max_features=1000, ngram_range=(1, 2), min_df=1, max_df=0.9)

        # Fit and transform documents
        _knowledge_vectors = _vectorizer.fit_transform(documents)


def semantic_search_knowledge(
    query: str, sources: list[dict[str, Any]], k: int = 10, min_similarity: float = 0.1
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """
    Perform semantic search on knowledge sources.

    Returns:
        Tuple of (search_results, extracted_entities)
    """
    # Build index if not already built
    build_knowledge_index(sources)

    # Extract entities from query
    entities = extract_entities(query)

    # Preprocess query
    processed_query = preprocess_text(query)

    # Simple keyword matching fallback
    keyword_matches = []

    for i, source in enumerate(sources):
        score = 0.0

        # Basic keyword matching
        searchable_text = f"{source['topic']} {source['title']} {source['note']}".lower()

        # Count query word matches
        query_words = processed_query.split()
        matches = sum(1 for word in query_words if word in searchable_text)

        if matches > 0:
            score = matches / len(query_words) if query_words else 0.0

            # Boost score for entity matches
            for entity in entities:
                if entity["text"].lower() in searchable_text:
                    score += 0.3

        if score >= min_similarity:
            result = source.copy()
            result["similarity_score"] = round(score, 4)
            keyword_matches.append(result)

    # If we have the TF-IDF vectors, use semantic similarity
    semantic_matches = []
    if _vectorizer is not None and _knowledge_vectors is not None:
        try:
            from sklearn.metrics.pairwise import cosine_similarity

            # Transform query
            query_vector = _vectorizer.transform([processed_query])

            # Calculate cosine similarities
            similarities = cosine_similarity(query_vector, _knowledge_vectors).flatten()

            # Get top matches
            for i, similarity in enumerate(similarities):
                if similarity >= min_similarity:
                    result = sources[i].copy()
                    result["similarity_score"] = round(float(similarity), 4)
                    semantic_matches.append(result)

        except (ValueError, TypeError, RuntimeError) as e:
            # ValueError: vectorizer transform issues; TypeError: shape mismatch;
            # RuntimeError: sklearn internal errors during cosine_similarity
            logger.warning(
                "tfidf_search_failed",
                query=query,
                error=str(e),
                error_type=type(e).__name__,
            )

    # Combine and deduplicate results
    all_matches = semantic_matches if semantic_matches else keyword_matches

    # Sort by similarity score
    all_matches.sort(key=lambda x: x["similarity_score"], reverse=True)

    # Return top k results
    return all_matches[:k], entities


def enhance_query_with_entities(query: str, entities: list[dict[str, str]]) -> str:
    """Enhance the original query with entity information."""
    if not entities:
        return query

    entity_info = []
    for entity in entities:
        entity_info.append(f"{entity['text']} ({entity['label']})")

    return f"{query} | Entities: {', '.join(entity_info)}"
