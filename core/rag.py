import uuid
import re
import os

try:
    import chromadb
except ImportError:
    chromadb = None

try:
    import tiktoken
except ImportError:
    tiktoken = None


def count_tokens(text: str) -> int:
    if tiktoken is None:
        # Fallback to rough estimation if tiktoken not installed
        return len(text) // 4
    enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text))

class RAGDB:
    """
    RAG(Retrieval-Augmented Generation)データベースの抽象化クラス。
    内部でChromaDBを使用する。
    """
    def __init__(self, db_directory: str, embedding_model: str = "multilingual-e5-small"):
        if chromadb is None:
            raise ImportError("chromadb is not installed. Please run `pip install chromadb`.")

        self.db_directory = db_directory
        os.makedirs(db_directory, exist_ok=True)

        self.client = chromadb.PersistentClient(path=db_directory)

        # multilingual-e5-small embedding function
        from sentence_transformers import SentenceTransformer
        import numpy as np
        # 二刀流: 同梱 models/ があればローカルからオフライン読み込み、無ければ HF キャッシュから解決
        from core.paths import resolve_model
        _model_src, _local_only = resolve_model("multilingual-e5-small", "intfloat/multilingual-e5-small")
        model = SentenceTransformer(_model_src, local_files_only=_local_only)

        class E5EmbeddingFunction:
            def name(self) -> str:
                return "multilingual-e5-small"
            def __call__(self, input: list[str]) -> list[list[float]]:
                texts = [f"query: {t}" for t in input]
                embeddings = model.encode(texts, normalize_embeddings=True)
                return embeddings.tolist()
            def embed_query(self, input: list[str]) -> list[list[float]]:
                texts = [f"query: {t}" for t in input]
                embeddings = model.encode(texts, normalize_embeddings=True)
                return embeddings.tolist()
            def embed_documents(self, input: list[str]) -> list[list[float]]:
                texts = [f"passage: {t}" for t in input]
                embeddings = model.encode(texts, normalize_embeddings=True)
                return embeddings.tolist()

        ef = E5EmbeddingFunction()
        self._ef = ef  # ← この1行を追加

        self.collections = {
            "logs": self.client.get_or_create_collection("logs", embedding_function=ef),
            "daily_memories": self.client.get_or_create_collection("daily_memories", embedding_function=ef),
            "notes": self.client.get_or_create_collection("notes", embedding_function=ef),
            "tool_results": self.client.get_or_create_collection("tool_results", embedding_function=ef),
        }
    
    def _chunk_text(self, text: str, min_tokens: int = 100, max_tokens: int = 1000) -> list[str]:
        """
        テキストをチャンクに分割する。
        - 基本はMarkdownの `## ` 見出しで分割
        - 見出しがない場合は空行(\n\n)で分割
        - それでも1000トークンを超える場合は段落(\n)で分割
        """
        chunks = []
        
        if "## " not in text:
            parts = re.split(r'\n{2,}', text)
        else:
            # ## を残したまま分割
            parts = re.split(r'(?=\n## )', text)
            
        current_chunk = ""
        
        for part in parts:
            part = part.strip()
            if not part:
                continue
                
            if count_tokens(part) > max_tokens:
                sub_parts = re.split(r'\n', part)
                for sub_part in sub_parts:
                    sub_part = sub_part.strip()
                    if not sub_part:
                        continue
                    if count_tokens(current_chunk + "\n" + sub_part) > max_tokens:
                        if count_tokens(current_chunk) >= min_tokens:
                            chunks.append(current_chunk.strip())
                            current_chunk = sub_part
                        else:
                            current_chunk += "\n" + sub_part
                    else:
                        current_chunk += "\n" + sub_part if current_chunk else sub_part
            else:
                if count_tokens(current_chunk + "\n" + part) > max_tokens:
                    if count_tokens(current_chunk) >= min_tokens:
                        chunks.append(current_chunk.strip())
                        current_chunk = part
                    else:
                        current_chunk += "\n" + part
                else:
                    current_chunk += "\n" + part if current_chunk else part
                    
        if current_chunk:
            chunks.append(current_chunk.strip())
            
        # 短すぎるチャンクの結合処理
        final_chunks = []
        temp_chunk = ""
        for c in chunks:
            if not temp_chunk:
                temp_chunk = c
            elif count_tokens(temp_chunk) < min_tokens:
                if count_tokens(temp_chunk + "\n" + c) <= max_tokens:
                    temp_chunk += "\n" + c
                else:
                    final_chunks.append(temp_chunk)
                    temp_chunk = c
            else:
                final_chunks.append(temp_chunk)
                temp_chunk = c
                
        if temp_chunk:
            final_chunks.append(temp_chunk)
            
        return final_chunks

    def add(self, collection_name: str, document: str, metadata: dict, doc_id: str = None) -> list[str]:
        """
        ドキュメントを追加する。
        内部でチャンク分割を行い、各チャンクをChromaDBに登録する。
        """
        if collection_name not in self.collections:
            raise ValueError(f"Unknown collection: {collection_name}")
            
        collection = self.collections[collection_name]
        
        chunks = self._chunk_text(document)
        
        ids = []
        documents = []
        metadatas = []
        
        base_id = doc_id or str(uuid.uuid4())
        
        for i, chunk in enumerate(chunks):
            chunk_id = f"{base_id}_{i}"
            ids.append(chunk_id)
            documents.append(chunk)
            
            chunk_meta = metadata.copy()
            # 見出しがあればメタデータに追加
            header_match = re.search(r'^##\s+(.+)$', chunk, re.MULTILINE)
            if header_match:
                chunk_meta['section'] = header_match.group(1).strip()
                
            # 文字列でないもの、不要なネストがある場合は除外(ChromaDB要件)
            clean_meta = {k: v for k, v in chunk_meta.items() if isinstance(v, (str, int, float, bool))}
            metadatas.append(clean_meta)
            
        if ids:
            collection.add(
                ids=ids,
                documents=documents,
                metadatas=metadatas
            )
            
        return ids

    def search(self, collection_name: str, query: str, n_results: int = 5) -> list[dict]:
        """
        クエリで類似するチャンクを検索する。
        """
        if collection_name not in self.collections:
            raise ValueError(f"Unknown collection: {collection_name}")
            
        collection = self.collections[collection_name]
        
        # count()をチェックし、空なら検索しない
        if collection.count() == 0:
            return []
            
        results = collection.query(
            query_texts=[query],
            n_results=min(n_results, collection.count())
        )
        
        formatted_results = []
        if results.get('documents') and results['documents'][0]:
            for i in range(len(results['documents'][0])):
                formatted_results.append({
                    'id': results['ids'][0][i],
                    'document': results['documents'][0][i],
                    'metadata': results['metadatas'][0][i] if results.get('metadatas') else {},
                    'distance': results['distances'][0][i] if results.get('distances') else 0.0
                })
                
        return formatted_results

    def delete(self, collection_name: str, doc_ids: list[str]):
        """
        IDを指定してドキュメントを削除する。
        """
        if collection_name not in self.collections:
            raise ValueError(f"Unknown collection: {collection_name}")
            
        collection = self.collections[collection_name]
        collection.delete(ids=doc_ids)

