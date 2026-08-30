#!/usr/bin/env python3
"""claude-history — archive + search YOUR claude.ai chat history in Postgres (Neon).

Sanctioned-data flow, no internal API: you export your data from claude.ai
(Settings -> Export data), and this tool imports conversations.json into a Neon
Postgres DB and searches it. Full-text now; semantic (pgvector) is a phase-2 add.

Mirrors the youtube-map-api DB conventions: DATABASE_URL via .env, a single
schema.sql, snake_case, bigint identity PKs, timestamptz, parameterized queries,
idempotent ON CONFLICT upserts.

Setup
  1. install the driver into the repo venv:
       .venv/Scripts/pip install "psycopg[binary]"
  2. .env:  DATABASE_URL=postgresql://USER:PASS@EP.neon.tech/DB?sslmode=require
  3. python claude_history.py init-db                    # create tables
  4. python claude_history.py import conversations.json  # load your export
  5. python claude_history.py search "naver map"         # full-text search

Commands
  init-db                          create the schema (idempotent)
  import <conversations.json>      load/refresh from a claude.ai export
  search <query> [--limit N] [--after YYYY-MM-DD] [--before YYYY-MM-DD]
  stats                            row counts + date range
"""
import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_FILE = SCRIPT_DIR / 'schema.sql'
try:
    from dotenv import load_dotenv
    load_dotenv(SCRIPT_DIR / '.env')
except Exception:
    pass

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()


def _connect():
    if not DATABASE_URL:
        sys.exit("Error: DATABASE_URL not set. Add your Neon connection string to .env:\n"
                 "  DATABASE_URL=postgresql://USER:PASS@EP.neon.tech/DB?sslmode=require")
    try:
        import psycopg
    except ImportError:
        sys.exit('psycopg not installed. Run:  .venv/Scripts/pip install "psycopg[binary]"')
    return psycopg.connect(DATABASE_URL)


# ---- export-shape handling (claude.ai export format has drifted over versions) ----
def _msg_text(m):
    """Extract plain text from a message across known export shapes."""
    c = m.get('content')
    if isinstance(c, list):
        parts = [b.get('text', '') for b in c
                 if isinstance(b, dict) and b.get('type') == 'text' and b.get('text')]
        if parts:
            return '\n'.join(parts)
    t = m.get('text')
    if isinstance(t, str) and t:
        return t
    if isinstance(c, list):  # last resort: any text-ish field in the blocks
        return '\n'.join(str(b.get('text', '')) for b in c if isinstance(b, dict) and b.get('text'))
    return ''


def _role(m):
    return m.get('sender') or m.get('role') or 'unknown'


def _conversations(data):
    """Normalize the top-level export into a list of conversation dicts."""
    if isinstance(data, dict):
        return data.get('conversations') or data.get('chat_conversations') or [data]
    return data if isinstance(data, list) else []


def cmd_init(args):
    sql = SCHEMA_FILE.read_text(encoding='utf-8')
    # Strip SQL line-comments (-- to end of line) BEFORE splitting on ';', so a
    # ';' inside a comment can't truncate a statement. Safe here: schema.sql has
    # no '--' or ';' inside string literals. psycopg3's extended protocol runs
    # ONE statement per execute(), hence the split.
    no_comments = '\n'.join(line.split('--', 1)[0] for line in sql.splitlines())
    statements = [s.strip() for s in no_comments.split(';') if s.strip()]
    with _connect() as conn, conn.cursor() as cur:
        for stmt in statements:
            cur.execute(stmt)
        conn.commit()
    print(f"schema applied ({len(statements)} statements) from {SCHEMA_FILE.name}")


CONV_UPSERT = """INSERT INTO conversations (conv_uuid, title, created_at, updated_at, message_count)
                 VALUES (%s, %s, %s, %s, %s)
                 ON CONFLICT (conv_uuid) DO UPDATE SET
                   title = EXCLUDED.title, updated_at = EXCLUDED.updated_at,
                   message_count = EXCLUDED.message_count, imported_at = now()"""
MSG_UPSERT = """INSERT INTO messages (msg_uuid, conv_uuid, seq, role, body, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (msg_uuid) DO UPDATE SET
                  body = EXCLUDED.body, role = EXCLUDED.role, seq = EXCLUDED.seq"""


def _rows_from_conversations(convos):
    """Flatten export/API conversations into (conv_rows, msg_rows) tuples."""
    conv_rows, msg_rows = [], []
    for conv in convos:
        cu = conv.get('uuid') or conv.get('conv_uuid')
        if not cu:
            continue
        msgs = conv.get('chat_messages') or conv.get('messages') or []
        conv_rows.append((cu, conv.get('name'), conv.get('created_at'),
                          conv.get('updated_at'), len(msgs)))
        for seq, m in enumerate(msgs):
            msg_rows.append((m.get('uuid'), cu, seq, _role(m), _msg_text(m), m.get('created_at')))
    return conv_rows, msg_rows


def cmd_import(args):
    data = json.loads(Path(args.file).read_text(encoding='utf-8'))
    conv_rows, msg_rows = _rows_from_conversations(_conversations(data))
    # Batched upserts via executemany (psycopg3 pipelines these) — one network
    # round-trip per batch instead of per row (16k singles to Singapore = ~20min).
    with _connect() as conn, conn.cursor() as cur:
        cur.executemany(CONV_UPSERT, conv_rows)
        cur.executemany(MSG_UPSERT, msg_rows)
        conn.commit()
    print(f"imported {len(conv_rows)} conversations, {len(msg_rows)} messages")


def cmd_search(args):
    q = args.query
    where = ["m.body_tsv @@ plainto_tsquery('simple', %s)"]
    params = [q]
    if args.after:
        where.append("m.created_at >= %s")
        params.append(args.after)
    if args.before:
        where.append("m.created_at <= %s")
        params.append(args.before)
    sql = f"""
        SELECT c.title, m.role, m.created_at, m.conv_uuid,
               ts_headline('simple', m.body, plainto_tsquery('simple', %s),
                           'MaxFragments=2, MaxWords=18, MinWords=5') AS snippet,
               ts_rank(m.body_tsv, plainto_tsquery('simple', %s)) AS rank
        FROM messages m
        JOIN conversations c ON c.conv_uuid = m.conv_uuid
        WHERE {' AND '.join(where)}
        ORDER BY rank DESC, m.created_at DESC
        LIMIT %s"""
    full_params = [q, q] + params + [args.limit]
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, full_params)
        rows = cur.fetchall()
    if not rows:
        print("no matches.")
        return
    print(f"{len(rows)} match(es) for {q!r}:")
    for title, role, created, cu, snippet, rank in rows:
        when = str(created)[:16] if created else '?'
        print(f"\n[{when}] {title or '(untitled)'}  ({role})  conv={cu[:8]}")
        print(f"   {' '.join((snippet or '').split())}")


def cmd_stats(args):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM conversations")
        nc = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM messages")
        nm = cur.fetchone()[0]
        cur.execute("SELECT min(created_at), max(created_at) FROM messages")
        lo, hi = cur.fetchone()
    print(f"conversations: {nc}\nmessages: {nm}\ndate range: {lo} .. {hi}")


# ---------- phase 2: semantic search (pgvector + local multilingual embeddings) ----------
EMBED_MODEL = 'BAAI/bge-m3'   # multilingual (KO+EN), 1024-dim, runs local on GPU
EMBED_DIM = 1024
_model = None

# DDL is idempotent; runs lazily the first time we embed (keeps full-text phase
# free of any pgvector dependency).
VEC_DDL = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    f"ALTER TABLE messages ADD COLUMN IF NOT EXISTS embedding vector({EMBED_DIM})",
    # HNSW index intentionally omitted: at <100k vectors a brute-force cosine scan is
    # sub-second, while the index (~125 MB + heavy bulk-load write-churn) blew the
    # free-tier 512 MB cap. Semantic search runs fine via a sequential scan; add the
    # index back only if this archive grows into the hundreds of thousands of messages.
]


def _load_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        import torch
        dev = 'cuda' if torch.cuda.is_available() else 'cpu'
        print(f"loading {EMBED_MODEL} on {dev} ...", file=sys.stderr)
        # Force safetensors: torch 2.5.1 blocks torch.load of .bin weights
        # (CVE-2025-32434). bge-m3 ships model.safetensors, so this sidesteps it.
        _model = SentenceTransformer(EMBED_MODEL, device=dev,
                                     model_kwargs={'use_safetensors': True})
    return _model


def _ensure_vector(conn):
    with conn.cursor() as cur:
        for stmt in VEC_DDL:
            cur.execute(stmt)
    conn.commit()
    from pgvector.psycopg import register_vector
    register_vector(conn)


def cmd_embed(args):
    import numpy as np
    # 1. Short connection: run DDL + fetch the rows, then RELEASE it. We must not
    #    hold a Neon connection open across the minutes-long GPU embed — it goes
    #    idle and the pooler drops the SSL connection.
    with _connect() as conn:
        _ensure_vector(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT id, body FROM messages "
                        "WHERE embedding IS NULL AND body <> '' ORDER BY id")
            rows = cur.fetchall()
    if not rows:
        print("nothing to embed (all messages already have embeddings).")
        return

    # 2. Embed on the GPU with NO DB connection held.
    print(f"loading model + embedding {len(rows)} messages on GPU ...")
    model = _load_model()
    ids = [r[0] for r in rows]
    vecs = model.encode([r[1] for r in rows], batch_size=args.batch,
                        normalize_embeddings=True, show_progress_bar=True)
    # Safety: persist vectors so a store failure never wastes the GPU work.
    np.savez(SCRIPT_DIR / '.embed_cache.npz', ids=np.array(ids), vecs=vecs)

    # 3. Fresh connection: store in committed chunks (robust + shows progress).
    with _connect() as conn:
        from pgvector.psycopg import register_vector
        register_vector(conn)
        CHUNK = 1000
        with conn.cursor() as cur:
            for i in range(0, len(ids), CHUNK):
                end = min(i + CHUNK, len(ids))
                cur.executemany("UPDATE messages SET embedding = %s WHERE id = %s",
                               [(vecs[j], ids[j]) for j in range(i, end)])
                conn.commit()
                print(f"  stored {end}/{len(ids)}")
    print(f"embedded {len(ids)} messages.")


def cmd_semantic(args):
    model = _load_model()
    qvec = model.encode([args.query], normalize_embeddings=True)[0]
    with _connect() as conn:
        from pgvector.psycopg import register_vector
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.title, m.role, m.created_at, m.conv_uuid,
                          left(m.body, 240) AS snippet,
                          m.embedding <=> %s AS dist
                   FROM messages m JOIN conversations c ON c.conv_uuid = m.conv_uuid
                   WHERE m.embedding IS NOT NULL
                   ORDER BY m.embedding <=> %s
                   LIMIT %s""",
                (qvec, qvec, args.limit))
            rows = cur.fetchall()
    if not rows:
        print("no embeddings yet — run `embed` first.")
        return
    print(f"top {len(rows)} semantic matches for {args.query!r}:")
    for title, role, created, cu, snippet, dist in rows:
        when = str(created)[:16] if created else '?'
        print(f"\n[{when}] {title or '(untitled)'}  ({role})  conv={cu[:8]}  sim={1 - float(dist):.3f}")
        print(f"   {' '.join((snippet or '').split())}")


# ---------- hybrid: fuse lexical + semantic rankings with Reciprocal Rank Fusion ----------
def cmd_hybrid(args):
    """Lexical (full-text) and semantic (vector) each produce a ranked list; RRF fuses
    them by RANK (score = sum 1/(k+rank)), so messages strong in BOTH float to the top.
    Pure Postgres — no extra index, no extra service. k=60 is the standard RRF default.
    The [src] tag shows L (lexical), S (semantic), or LS (both = consensus)."""
    K = 60
    pool = max(args.limit * 5, 50)  # rank deeper than the final cut for a good fusion
    model = _load_model()
    qvec = model.encode([args.query], normalize_embeddings=True)[0]
    with _connect() as conn:
        from pgvector.psycopg import register_vector
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT m.msg_uuid FROM messages m
                           WHERE m.body_tsv @@ plainto_tsquery('simple', %s)
                           ORDER BY ts_rank_cd(m.body_tsv, plainto_tsquery('simple', %s)) DESC,
                                    m.created_at DESC
                           LIMIT %s""", (args.query, args.query, pool))
            lex = [r[0] for r in cur.fetchall()]
            cur.execute("""SELECT m.msg_uuid FROM messages m
                           WHERE m.embedding IS NOT NULL
                           ORDER BY m.embedding <=> %s
                           LIMIT %s""", (qvec, pool))
            sem = [r[0] for r in cur.fetchall()]
            scores = {}
            for rank, mid in enumerate(lex, 1):
                scores[mid] = scores.get(mid, 0.0) + 1.0 / (K + rank)
            for rank, mid in enumerate(sem, 1):
                scores[mid] = scores.get(mid, 0.0) + 1.0 / (K + rank)
            top = sorted(scores, key=scores.get, reverse=True)[:args.limit]
            if not top:
                print("no matches.")
                return
            cur.execute("""SELECT m.msg_uuid, c.title, m.role, m.created_at, m.conv_uuid,
                                  left(m.body, 240) AS snippet
                           FROM messages m JOIN conversations c ON c.conv_uuid = m.conv_uuid
                           WHERE m.msg_uuid = ANY(%s)""", (top,))
            info = {r[0]: r[1:] for r in cur.fetchall()}
    lexset, semset = set(lex), set(sem)
    print(f"top {len(top)} hybrid (RRF) matches for {args.query!r}:")
    for mid in top:
        title, role, created, cu, snippet = info[mid]
        src = ('L' if mid in lexset else '') + ('S' if mid in semset else '')
        when = str(created)[:16] if created else '?'
        print(f"\n[{when}] {title or '(untitled)'}  ({role})  conv={str(cu)[:8]}  rrf={scores[mid]:.4f} [{src or '-'}]")
        print(f"   {' '.join((snippet or '').split())}")


# ---------- sync: pull new/updated chats straight from claude.ai (no manual export) ----------
# claude.ai's web API sits behind Cloudflare, which 403s bare clients — so we send
# browser-shaped headers. Auth is your sessionKey cookie (CLAUDE_SESSION_KEY in .env).
# Incremental: list conversations, refetch only those new or whose updated_at advanced,
# then reuse the same idempotent upsert path the export importer uses.
CLAUDE_SESSION_KEY = os.environ.get('CLAUDE_SESSION_KEY', '').strip()
CLAUDE_ORG_UUID = os.environ.get('CLAUDE_ORG_UUID', '').strip()
_CAI_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
           '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')


def _cai_get(path):
    import urllib.request
    if not CLAUDE_SESSION_KEY:
        sys.exit("Error: CLAUDE_SESSION_KEY not set in .env (your claude.ai sessionKey cookie).")
    req = urllib.request.Request('https://claude.ai' + path, headers={
        'Cookie': f'sessionKey={CLAUDE_SESSION_KEY}', 'User-Agent': _CAI_UA,
        'Accept': 'application/json', 'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://claude.ai/'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _cai_org():
    if CLAUDE_ORG_UUID:
        return CLAUDE_ORG_UUID
    orgs = _cai_get('/api/organizations')
    orgs = orgs if isinstance(orgs, list) else [orgs]
    if not orgs:
        sys.exit("No organizations for this session key (is it still valid?).")
    return orgs[0]['uuid']


def _cai_list_conversations(org):
    """Page the conversation list (metadata incl. updated_at). Safe against an
    endpoint that ignores ?offset: stops when a page yields nothing new."""
    import time
    out, seen, offset, page = [], set(), 0, 100
    while True:
        batch = _cai_get(f'/api/organizations/{org}/chat_conversations?limit={page}&offset={offset}')
        batch = batch if isinstance(batch, list) else batch.get('conversations', [])
        fresh = [c for c in batch if c.get('uuid') and c['uuid'] not in seen]
        if not fresh:
            break
        for c in fresh:
            seen.add(c['uuid'])
        out.extend(fresh)
        if len(batch) < page:
            break
        offset += page
        time.sleep(0.3)
    return out


def _parse_ts(s):
    from datetime import datetime
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None


def cmd_sync(args):
    import time
    org = _cai_org()
    print(f"claude.ai org {org}\nlisting conversations ...")
    remote = _cai_list_conversations(org)
    print(f"  {len(remote)} conversations on claude.ai")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT conv_uuid, updated_at FROM conversations")
        have = {str(u): ts for u, ts in cur.fetchall()}
    todo = []
    for c in remote:
        cu = c.get('uuid')
        if not cu:
            continue
        if cu not in have:
            todo.append(c)                                    # brand new
        else:
            rt, lt = _parse_ts(c.get('updated_at')), have[cu]
            try:
                if rt and lt and rt > lt:
                    todo.append(c)                            # updated since last sync
            except TypeError:
                todo.append(c)                                # tz naive/aware mismatch — refetch
    if args.limit:
        todo = todo[:args.limit]
    print(f"  {len(todo)} new/updated to pull")
    if not todo:
        print("already up to date.")
        return
    conv_rows, msg_rows, ok = [], [], 0
    for c in todo:
        cu = c['uuid']
        try:
            full = _cai_get(f'/api/organizations/{org}/chat_conversations/{cu}')
        except Exception as e:
            print(f"  ! skip {cu[:8]}: {e}")
            continue
        cr, mr = _rows_from_conversations([full])
        conv_rows.extend(cr)
        msg_rows.extend(mr)
        ok += 1
        if ok % 20 == 0:
            print(f"  fetched {ok}/{len(todo)} ...")
        time.sleep(args.delay)
    with _connect() as conn, conn.cursor() as cur:
        cur.executemany(CONV_UPSERT, conv_rows)
        cur.executemany(MSG_UPSERT, msg_rows)
        conn.commit()
    print(f"synced {len(conv_rows)} conversations, {len(msg_rows)} messages")
    if getattr(args, 'embed', False):
        print("embedding new messages ...")
        cmd_embed(args)


def main():
    p = argparse.ArgumentParser(description="Archive + search your claude.ai chat history in Neon Postgres.")
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('init-db', help='create the schema (idempotent)')
    pi = sub.add_parser('import', help='load/refresh from a claude.ai export')
    pi.add_argument('file', help='path to conversations.json from the export')
    ps = sub.add_parser('search', help='full-text search')
    ps.add_argument('query')
    ps.add_argument('--limit', type=int, default=20)
    ps.add_argument('--after', help='YYYY-MM-DD')
    ps.add_argument('--before', help='YYYY-MM-DD')
    sub.add_parser('stats', help='row counts + date range')
    pe = sub.add_parser('embed', help='generate vector embeddings (pgvector + GPU)')
    pe.add_argument('--batch', type=int, default=32)
    psm = sub.add_parser('semantic', help='semantic (meaning-based) search')
    psm.add_argument('query')
    psm.add_argument('--limit', type=int, default=10)
    ph = sub.add_parser('hybrid', help='hybrid search: lexical + semantic fused with RRF')
    ph.add_argument('query')
    ph.add_argument('--limit', type=int, default=10)
    py = sub.add_parser('sync', help='pull new/updated chats straight from claude.ai (no export)')
    py.add_argument('--limit', type=int, default=0, help='cap conversations fetched this run (0 = all new/updated)')
    py.add_argument('--delay', type=float, default=0.4, help='seconds between conversation fetches (be polite)')
    py.add_argument('--embed', action='store_true', help='also embed new messages after syncing')
    py.add_argument('--batch', type=int, default=32)
    args = p.parse_args()
    {'init-db': cmd_init, 'import': cmd_import, 'search': cmd_search, 'stats': cmd_stats,
     'embed': cmd_embed, 'semantic': cmd_semantic, 'hybrid': cmd_hybrid, 'sync': cmd_sync}[args.cmd](args)


if __name__ == '__main__':
    main()
