#!/usr/bin/env python3
"""
index.db のチャンクを多言語埋め込みモデルでベクトル化する（NVIDIA GPU 推奨）。

  python3 embed_index.py <コーパス> [--model BAAI/bge-m3] [--batch 64]

- 出力: <コーパス>/vectors.f16（float16 の行列）, vectors.json（モデル名・次元・進捗）,
        vectors.keys.npy（各行の本文のハッシュ。索引を作り直したときの使い回しに使う）
- 途中で止めても、同じコマンドで続きから再開できる
- 資料を足して索引を作り直したあとは、本文が同じチャンクのベクトルを前回の結果から写し、
  新しいチャンクだけを計算する（前回のベクトル化が最後まで終わっていて、vectors.keys.npy がある場合）
- 初回はモデルを Hugging Face からダウンロードする（以後はオフラインで動く）

必要なもの: pip install sentence-transformers  （CUDA 版 PyTorch を先に入れておく）
"""
import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

from embedder import Embedder


def text_key(text):
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "little")


def all_keys(con, n):
    keys = np.zeros(n, dtype=np.uint64)
    for cid, text in con.execute("SELECT id, text FROM chunks ORDER BY id"):
        keys[cid - 1] = text_key(text)
    return keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("--model", default="BAAI/bge-m3",
                    help="既定は bge-m3（日本語の問いで英語の本文に当たりやすい。2026-11-04 の比較）。"
                         "速さ重視なら intfloat/multilingual-e5-base（約4倍速いが精度は落ちる）")
    ap.add_argument("--batch", type=int, default=64, help="GPU メモリが足りなければ下げる")
    a = ap.parse_args()

    corpus = Path(a.corpus).resolve()
    con = sqlite3.connect(corpus / "index.db")
    n = con.execute("SELECT MAX(id) FROM chunks").fetchone()[0] or 0
    meta_path, vec_path, key_path = corpus / "vectors.json", corpus / "vectors.f16", corpus / "vectors.keys.npy"

    emb = Embedder(a.model)
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else None
    if meta and meta["model"] != a.model:
        sys.exit("既存の vectors.* は別のモデルのものです。削除してから再実行してください")

    old = None
    if meta and meta["rows"] != n:
        sys.exit("vectors.* が今の index.db と合いません（索引を作り直した後に残ったもの）。削除してから再実行してください")
    if not meta:
        # 索引が作り直された直後。build_index.py が前回の結果を vectors.prev.* に移している。
        # 前回が最後まで終わっていれば、本文が同じチャンクのベクトルを写す
        pm, pv, pk = corpus / "vectors.prev.json", corpus / "vectors.prev.f16", corpus / "vectors.prev.keys.npy"
        if pm.exists():
            prev = json.loads(pm.read_text())
            if prev["model"] == a.model and prev["done"] >= prev["rows"] and pk.exists() and pv.exists():
                old = (np.load(pk), np.memmap(pv, dtype=np.float16, mode="r", shape=(prev["rows"], prev["dim"])))
            else:
                print("前回のベクトル化が途中・別のモデル・本文のハッシュ無しのいずれかなので、全部を計算します",
                      file=sys.stderr)

    if not meta:
        meta = {"model": a.model, "dim": emb.dim, "rows": n, "done": 0}
        np.memmap(vec_path, dtype=np.float16, mode="w+", shape=(n, emb.dim)).flush()
        keys = all_keys(con, n)
        np.save(key_path, keys)
        reused = np.zeros(n, dtype=bool)
        if old is not None:
            old_keys, old_vecs = old
            where = {int(k): i for i, k in enumerate(old_keys)}
            src = np.array([where.get(int(k), -1) for k in keys])
            reused = src >= 0
            vecs = np.memmap(vec_path, dtype=np.float16, mode="r+", shape=(n, emb.dim))
            idx = np.nonzero(reused)[0]
            for i in range(0, len(idx), 50000):   # 少しずつ写す（一度に読むとメモリを食う）
                part = idx[i:i + 50000]
                vecs[part] = old_vecs[src[part]]
            vecs.flush()
            del vecs, old_vecs
            old = None   # 前回のファイルを閉じる（最後に消すため）
            print(f"前回の結果から {reused.sum()} 件を写しました（新しく計算するのは {n - reused.sum()} 件）",
                  file=sys.stderr)
        np.save(corpus / "vectors.reused.npy", reused)
        meta_path.write_text(json.dumps(meta))
    reused_path = corpus / "vectors.reused.npy"
    reused = np.load(reused_path) if reused_path.exists() else np.zeros(n, dtype=bool)
    vecs = np.memmap(vec_path, dtype=np.float16, mode="r+", shape=(n, meta["dim"]))

    start, t0, computed = meta["done"], time.time(), 0
    print(f"{n} チャンク中 {start} 件済み。デバイス: {emb.device}", file=sys.stderr)
    step = a.batch * 20
    for lo in range(start, n, step):
        rows = [r for r in con.execute("SELECT id, text FROM chunks WHERE id > ? AND id <= ? ORDER BY id",
                                       (lo, lo + step)) if not reused[r[0] - 1]]
        if rows:
            ids = np.array([r[0] for r in rows]) - 1   # chunk id は 1 始まり、行は 0 始まり
            vecs[ids] = emb.passages([r[1] for r in rows], a.batch).astype(np.float16)
            computed += len(rows)
        meta["done"] = min(lo + step, n)
        vecs.flush()
        meta_path.write_text(json.dumps(meta))
        rate = computed / max(time.time() - t0, 1e-9)
        left = int((~reused[meta["done"]:]).sum())
        eta = left / max(rate, 1e-9) if computed else 0
        print(f"  {meta['done']}/{n}  {rate:.0f} 件/秒  残り約 {eta / 3600:.1f} 時間", file=sys.stderr)
    reused_path.unlink(missing_ok=True)
    for f in ("vectors.prev.f16", "vectors.prev.json", "vectors.prev.keys.npy"):
        (corpus / f).unlink(missing_ok=True)   # 使い回しが済んだので前回の分は要らない
    if not key_path.exists():   # この仕組みより前に始めたベクトル化の続きだった場合
        np.save(key_path, all_keys(con, n))
    print("完了", file=sys.stderr)


if __name__ == "__main__":
    main()
