#!/usr/bin/env python3
"""
extract.py で作ったコーパスから、検索用の SQLite データベースを作る。

  python3 build_index.py <コーパス>            # <コーパス>/index.db を作り直す

- ページを ~1200 字前後のチャンクに分割（前のチャンクと少し重ねる）
- SQLite FTS5 の全文検索索引（BM25 で関連度順に並ぶ）を作る
- ベクトル索引は、この後 embed_index.py で作る
標準ライブラリのみで動く。
"""
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

TARGET = 1200   # チャンクの目安文字数
OVERLAP = 200   # 前チャンク末尾から引き継ぐ文字数の目安
MIN_PAGE = 30   # これ未満の文字数のページは捨てる（白紙・ロゴだけ等）
PAGE_RE = re.compile(r"^=== PAGE (\d+) ===$")

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE docs(id INTEGER PRIMARY KEY, path TEXT UNIQUE, product TEXT, version TEXT, form TEXT,
    title TEXT, pages INTEGER);
CREATE TABLE chunks(id INTEGER PRIMARY KEY, doc_id INTEGER, page INTEGER, text TEXT);
CREATE INDEX chunks_doc_page ON chunks(doc_id, page);
-- z/OS の名前に使われる $ # @ を語の一部として扱う（$HASP373, SYS1 など）
CREATE VIRTUAL TABLE fts USING fts5(text, content='chunks', content_rowid='id',
    tokenize="unicode61 tokenchars '$#@'");
"""


def iter_pages(path: Path):
    page, buf = None, []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = PAGE_RE.match(line.rstrip("\n"))
            if m:
                if page is not None:
                    yield page, buf
                page, buf = int(m.group(1)), []
            else:
                buf.append(line.rstrip())
    if page is not None:
        yield page, buf


def chunk_page(lines):
    # 空行の連続を1つにまとめる
    clean, blank = [], False
    for ln in lines:
        if ln.strip():
            clean.append(ln)
            blank = False
        elif not blank:
            clean.append("")
            blank = True
    text = "\n".join(clean).strip()
    if len(text) < MIN_PAGE:
        return []
    if len(text) <= TARGET * 1.5:
        return [text]

    chunks, cur, size = [], [], 0
    for ln in text.split("\n"):
        cur.append(ln)
        size += len(ln) + 1
        if size >= TARGET:
            chunks.append("\n".join(cur).strip())
            keep, kept = [], 0
            for prev in reversed(cur):
                if kept >= OVERLAP:
                    break
                keep.insert(0, prev)
                kept += len(prev) + 1
            cur, size = keep, kept
    tail = "\n".join(cur).strip()
    if tail and (not chunks or len(tail) > OVERLAP * 1.2):
        chunks.append(tail)
    return chunks


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    corpus = Path(sys.argv[1]).resolve()
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    db_path = corpus / "index.db"
    tmp = corpus / "index.db.building"
    tmp.unlink(missing_ok=True)

    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    t0, n_chunks = time.time(), 0
    for i, (key, m) in enumerate(sorted(manifest.items()), 1):
        cur = con.execute(
            "INSERT INTO docs(path, product, version, form, title, pages) VALUES (?,?,?,?,?,?)",
            (key, m.get("product"), m.get("version"), m.get("form"), m.get("title"), m.get("pages")))
        doc_id = cur.lastrowid
        rows = [(doc_id, page, c)
                for page, lines in iter_pages(corpus / key)
                for c in chunk_page(lines)]
        con.executemany("INSERT INTO chunks(doc_id, page, text) VALUES (?,?,?)", rows)
        n_chunks += len(rows)
        if i % 100 == 0 or i == len(manifest):
            con.commit()
            print(f"  {i}/{len(manifest)} 文書, {n_chunks} チャンク", file=sys.stderr)

    print("全文検索索引を構築中…", file=sys.stderr)
    con.execute("INSERT INTO fts(fts) VALUES('rebuild')")
    con.execute("INSERT INTO fts(fts) VALUES('optimize')")
    con.execute("INSERT INTO meta VALUES('built_at', ?)", (time.strftime("%Y-%m-%d %H:%M:%S"),))
    con.commit()
    con.close()
    try:
        tmp.replace(db_path)
    except PermissionError:
        sys.exit(f"{db_path} を置き換えられません。検索サーバー（hsearch.py serve）が使っている可能性があります。"
                 f"止めてから実行し直してください。作った索引は {tmp} に残っています")

    # 索引を作り直したので、古いベクトルは検索には使えない（チャンクの番号が変わる）。
    # 消さずに vectors.prev.* に移し、embed_index.py が本文の同じチャンクのベクトルを使い回せるようにする
    names = {"vectors.f16": "vectors.prev.f16", "vectors.json": "vectors.prev.json",
             "vectors.keys.npy": "vectors.prev.keys.npy"}
    if (corpus / "vectors.json").exists():
        for f in names.values():
            (corpus / f).unlink(missing_ok=True)
    for f, to in names.items():
        try:
            if (corpus / f).exists():
                (corpus / f).replace(corpus / to)
        except PermissionError:
            sys.exit(f"{corpus / f} を動かせません。検索サーバー（hsearch.py serve）を止めてから、"
                     "このファイルを消して embed_index.py を実行してください")
    (corpus / "vectors.reused.npy").unlink(missing_ok=True)
    print(f"完了: {n_chunks} チャンク, {time.time() - t0:.0f} 秒 → {db_path}", file=sys.stderr)
    print("次に embed_index.py でベクトル索引を作ってください", file=sys.stderr)


if __name__ == "__main__":
    main()
