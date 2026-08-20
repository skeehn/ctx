"""
Cross-encoder Reranking for ctx-vault Search
Improves search relevance by reranking FTS5 results with a cross-encoder model.
"""
import os
import logging
from typing import List, Dict, Any, Optional
from functools import lru_cache

logger = logging.getLogger(__name__)

# Optional cross-encoder (lazy load)
_CROSS_ENCODER = None
_CROSS_ENCODER_MODEL = os.environ.get("CTX_RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")


def _get_cross_encoder():
    """Lazy load cross-encoder model."""
    global _CROSS_ENCODER
    if _CROSS_ENCODER is None:
        try:
            from sentence_transformers import CrossEncoder
            _CROSS_ENCODER = CrossEncoder(_CROSS_ENCODER_MODEL, max_length=512)
            logger.info(f"Loaded cross-encoder: {_CROSS_ENCODER_MODEL}")
        except ImportError:
            logger.warning("sentence-transformers not installed. Cross-encoder reranking disabled.")
            return None
        except Exception as e:
            logger.error(f"Failed to load cross-encoder: {e}")
            return None
    return _CROSS_ENCODER


def rerank_results(
    query: str,
    results: List[Dict[str, Any]],
    top_k: int = 10,
    model_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Rerank search results using a cross-encoder model.
    
    Args:
        query: The original search query
        results: List of search results from FTS5/BM25
        top_k: Number of top results to return after reranking
        model_name: Optional override for cross-encoder model
    
    Returns:
        Reranked results with updated scores
    """
    if not results:
        return []
    
    cross_encoder = _get_cross_encoder()
    if cross_encoder is None:
        logger.debug("Cross-encoder not available, returning original results")
        return results[:top_k]
    
    try:
        # Prepare pairs for cross-encoder: (query, document)
        pairs = []
        for result in results:
            # Combine title and snippet for reranking
            doc_text = f"{result.get('title', '')}. {result.get('snippet', '')}"
            pairs.append([query, doc_text])
        
        # Get cross-encoder scores
        scores = cross_encoder.predict(pairs, show_progress_bar=False)
        
        # Attach scores to results
        for result, score in zip(results, scores):
            result['rerank_score'] = float(score)
            # Keep original BM25 score as 'bm25_score'
            if 'score' in result and 'rerank_score' not in result:
                result['bm25_score'] = result['score']
            result['score'] = float(score)  # Update primary score to rerank score
        
        # Sort by rerank score (higher is better for cross-encoder)
        reranked = sorted(results, key=lambda x: x.get('rerank_score', 0), reverse=True)
        
        logger.debug(f"Reranked {len(results)} results, returning top {top_k}")
        return reranked[:top_k]
        
    except Exception as e:
        logger.error(f"Reranking failed: {e}")
        return results[:top_k]


def hybrid_rerank(
    query: str,
    results: List[Dict[str, Any]],
    top_k: int = 10,
    bm25_weight: float = 0.3,
    rerank_weight: float = 0.7,
) -> List[Dict[str, Any]]:
    """
    Hybrid reranking combining BM25 and cross-encoder scores.
    
    Args:
        query: The original search query
        results: List of search results from FTS5/BM25
        top_k: Number of top results to return
        bm25_weight: Weight for BM25 score (0-1)
        rerank_weight: Weight for cross-encoder score (0-1)
    
    Returns:
        Reranked results with combined scores
    """
    if not results:
        return []
    
    # First get cross-encoder scores
    cross_encoder = _get_cross_encoder()
    if cross_encoder is None:
        return results[:top_k]
    
    try:
        # Get cross-encoder scores
        pairs = []
        for result in results:
            doc_text = f"{result.get('title', '')}. {result.get('snippet', '')}"
            pairs.append([query, doc_text])
        
        rerank_scores = cross_encoder.predict(pairs, show_progress_bar=False)
        
        # Normalize BM25 scores (they're negative, lower is better)
        bm25_scores = [r.get('score', 0) for r in results]
        if bm25_scores:
            min_bm25 = min(bm25_scores)
            max_bm25 = max(bm25_scores)
            if max_bm25 > min_bm25:
                bm25_normalized = [(s - min_bm25) / (max_bm25 - min_bm25) for s in bm25_scores]
            else:
                bm25_normalized = [0.5] * len(bm25_scores)
        else:
            bm25_normalized = [0.5] * len(results)
        
        # Normalize rerank scores (higher is better)
        min_rerank = min(rerank_scores)
        max_rerank = max(rerank_scores)
        if max_rerank > min_rerank:
            rerank_normalized = [(s - min_rerank) / (max_rerank - min_rerank) for s in rerank_scores]
        else:
            rerank_normalized = [0.5] * len(results)
        
        # Combine scores
        for i, result in enumerate(results):
            combined = (
                bm25_weight * bm25_normalized[i] + 
                rerank_weight * rerank_normalized[i]
            )
            result['bm25_score'] = result.get('score', 0)
            result['rerank_score'] = float(rerank_scores[i])
            result['combined_score'] = float(combined)
            result['score'] = float(combined)  # Use combined as primary
        
        # Sort by combined score
        hybrid = sorted(results, key=lambda x: x.get('combined_score', 0), reverse=True)
        
        return hybrid[:top_k]
        
    except Exception as e:
        logging.getLogger(__name__).error(f"Hybrid reranking failed: {e}")
        return results[:top_k]


def search_with_rerank(
    query: str,
    initial_results: List[Dict[str, Any]],
    top_k: int = 10,
    use_hybrid: bool = True,
) -> List[Dict[str, Any]]:
    """
    Main entry point for reranked search.
    
    Args:
        query: Search query
        initial_results: Initial results from FTS5/BM25
        top_k: Number of results to return
        use_hybrid: Whether to use hybrid (BM25 + rerank) or pure rerank
    
    Returns:
        Reranked results
    """
    if not initial_results:
        return []
    
    # Limit initial results for reranking (performance)
    max_rerank = min(len(initial_results), 50)
    candidates = initial_results[:max_rerank]
    
    if use_hybrid:
        return hybrid_rerank(query, candidates, top_k=top_k)
    else:
        return rerank_results(query, candidates, top_k=top_k)


# For testing
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG)
    
    # Mock results for testing
    mock_results = [
        {"path": "note1.ctx", "title": "Python Async", "snippet": "asyncio.gather runs coroutines concurrently", "score": -2.5, "chunk_type": "text"},
        {"path": "note2.ctx", "title": "Database", "snippet": "Connection pooling with psycopg2", "score": -3.1, "chunk_type": "text"},
        {"path": "note3.ctx", "title": "Network", "snippet": "TCP vs UDP comparison", "score": -4.2, "chunk_type": "text"},
    ]
    
    query = "asyncio gather concurrent"
    
    print("Original results:")
    for r in mock_results:
        print(f"  {r['title']}: {r['score']}")
    
    reranked = search_with_rerank(query, mock_results, top_k=3, use_hybrid=True)
    
    print("\nReranked results:")
    for r in reranked:
        print(f"  {r['title']}: combined={r.get('combined_score', 0):.3f}, rerank={r.get('rerank_score', 0):.3f}, bm25={r.get('bm25_score', 0):.3f}")