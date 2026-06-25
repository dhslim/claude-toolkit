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
    args = p.parse_args()
    {'init-db': cmd_init, 'import': cmd_import, 'search': cmd_search, 'stats': cmd_stats}[args.cmd](args)


if __name__ == '__main__':
    main()
