import json
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

_collection = None

# 현재 25개 수록 (데모 수준). 운영 전 식품안전처 DB에서 300개 이상으로 확장 필요.
_KB_PATH = Path(__file__).parent.parent / "data" / "nutrition_kb.json"
_CHROMA_PATH = str(Path(__file__).parent.parent.parent / "chroma_db")
_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

# ChromaDB 기본 거리(L2, 낮을수록 유사)가 이 값을 초과하면 컨텍스트에서 제외.
# 초기값은 경험적 추정치 — 우선순위1 retrieval 품질 평가 스크립트로 추후 튜닝 필요.
_RELEVANCE_DISTANCE_THRESHOLD = 1.5


def _get_collection():
    global _collection
    if _collection is not None:
        return _collection

    ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=_MODEL_NAME
    )
    client = chromadb.PersistentClient(path=_CHROMA_PATH)
    col = client.get_or_create_collection("nutrition", embedding_function=ef)

    if col.count() == 0:
        _load_kb(col)

    _collection = col
    return _collection


def _load_kb(collection) -> None:
    with open(_KB_PATH, encoding="utf-8") as f:
        items = json.load(f)
    collection.add(
        documents=[item["document"] for item in items],
        ids=[item["id"] for item in items],
        metadatas=[{"name": item["name"], "info": item["info"]} for item in items],
    )


def search(query: str, n_results: int = 3) -> list[dict]:
    """
    쿼리와 의미적으로 유사한 식품 문서를 ChromaDB에서 검색한다.
    반환: [{"name": str, "info": str, "document": str}]
    """
    collection = _get_collection()
    count = collection.count()
    if count == 0:
        return []

    results = collection.query(
        query_texts=[query],
        n_results=min(n_results, count),
        include=["documents", "metadatas", "distances"],
    )
    docs = results["documents"][0]
    metas = results["metadatas"][0]
    distances = results["distances"][0]
    return [
        {"name": m["name"], "info": m["info"], "document": d}
        for d, m, dist in zip(docs, metas, distances)
        if dist <= _RELEVANCE_DISTANCE_THRESHOLD
    ]


def reset_collection_for_test() -> None:
    """테스트용 컬렉션 초기화 함수."""
    global _collection
    _collection = None
