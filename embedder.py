"""
埋め込みモデルの薄いラッパー。embed_index.py と hsearch.py が共有する。

E5 系モデルは、検索文に "query: "、本文に "passage: " を付ける約束がある。
環境変数 ZOS_DUMMY_EMBED=1 で、モデル無しの動作確認用ダミーになる。
"""
import hashlib
import os

import numpy as np


class Embedder:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self.e5 = "e5" in model_name.lower()
        if os.environ.get("ZOS_DUMMY_EMBED"):
            self.model, self.dim, self.device = None, 64, "dummy"
            return
        import torch
        from sentence_transformers import SentenceTransformer
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = SentenceTransformer(model_name, device=self.device)
        # チャンクは約1200字（数百トークン）なので512で足りる。bge-m3 の既定 8192 のままだと遅く、メモリも食う
        if self.model.max_seq_length > 512:
            self.model.max_seq_length = 512
        if self.device == "cuda":
            self.model.half()
        self.dim = self.model.get_sentence_embedding_dimension()

    def _encode(self, texts, batch):
        if self.model is None:  # ダミー: 単語のハッシュを足し合わせただけのベクトル
            out = np.zeros((len(texts), self.dim), dtype=np.float32)
            for i, t in enumerate(texts):
                for w in t.lower().split():
                    out[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1
            out /= np.linalg.norm(out, axis=1, keepdims=True) + 1e-9
            return out
        return self.model.encode(texts, batch_size=batch, normalize_embeddings=True,
                                 convert_to_numpy=True, show_progress_bar=False)

    def passages(self, texts, batch=256):
        return self._encode([("passage: " + t) if self.e5 else t for t in texts], batch)

    def query(self, text):
        return self._encode([("query: " + text) if self.e5 else text], 1)[0]
