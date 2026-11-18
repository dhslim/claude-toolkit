#!/usr/bin/env python3
"""Minimal PRIVATE web wrapper for claude-history search.

Reuses claude_history's DB connection + bge-m3 embedding model. Serves a tiny search
page at / and JSON at /api/{search|semantic|hybrid}. Same SQL the CLI uses, so a query
here returns the same hits as `python claude_history.py hybrid "..."`.

Run (local):
  .venv/Scripts/python.exe -m uvicorn claude_search_web:app --host 127.0.0.1 --port 8800
Then open http://127.0.0.1:8800

DEPLOY NOTE: this binds to localhost only. Public access goes through Cloudflare
Tunnel + Cloudflare Access (passkey) — never expose this port directly.
"""
import claude_history as ch
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Claude Chat Search")

_model = None
def _embed(q):
    global _model
    if _model is None:
        _model = ch._load_model()
    return _model.encode([q], normalize_embeddings=True)[0]


def _fmt(r):
    """r = (msg_uuid, title, role, created_at, conv_uuid, snippet)."""
    return {
        "msg_uuid": str(r[0]), "title": r[1] or "(untitled)", "role": r[2],
        "created_at": str(r[3])[:16] if r[3] else None, "conv_uuid": str(r[4]),
        "snippet": " ".join((r[5] or "").split()),
    }


def lexical(q, limit):
    sql = """SELECT m.msg_uuid, c.title, m.role, m.created_at, m.conv_uuid,
                    ts_headline('simple', m.body, plainto_tsquery('simple', %s),
                                'MaxFragments=2, MaxWords=18, MinWords=5') AS snippet
             FROM messages m JOIN conversations c ON c.conv_uuid = m.conv_uuid
             WHERE m.body_tsv @@ plainto_tsquery('simple', %s)
             ORDER BY ts_rank_cd(m.body_tsv, plainto_tsquery('simple', %s)) DESC, m.created_at DESC
             LIMIT %s"""
    with ch._connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (q, q, q, limit))
        return [_fmt(r) for r in cur.fetchall()]


def semantic(q, limit):
    qvec = _embed(q)
    with ch._connect() as conn:
        from pgvector.psycopg import register_vector
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT m.msg_uuid, c.title, m.role, m.created_at, m.conv_uuid,
                                  left(m.body, 240) AS snippet
                           FROM messages m JOIN conversations c ON c.conv_uuid = m.conv_uuid
                           WHERE m.embedding IS NOT NULL
                           ORDER BY m.embedding <=> %s LIMIT %s""", (qvec, limit))
            return [_fmt(r) for r in cur.fetchall()]


def hybrid(q, limit):
    K, pool = 60, max(limit * 5, 50)
    qvec = _embed(q)
    with ch._connect() as conn:
        from pgvector.psycopg import register_vector
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT m.msg_uuid FROM messages m
                           WHERE m.body_tsv @@ plainto_tsquery('simple', %s)
                           ORDER BY ts_rank_cd(m.body_tsv, plainto_tsquery('simple', %s)) DESC,
                                    m.created_at DESC LIMIT %s""", (q, q, pool))
            lex = [r[0] for r in cur.fetchall()]
            cur.execute("""SELECT m.msg_uuid FROM messages m WHERE m.embedding IS NOT NULL
                           ORDER BY m.embedding <=> %s LIMIT %s""", (qvec, pool))
            sem = [r[0] for r in cur.fetchall()]
            scores = {}
            for i, mid in enumerate(lex, 1):
                scores[mid] = scores.get(mid, 0.0) + 1.0 / (K + i)
            for i, mid in enumerate(sem, 1):
                scores[mid] = scores.get(mid, 0.0) + 1.0 / (K + i)
            top = sorted(scores, key=scores.get, reverse=True)[:limit]
            if not top:
                return []
            cur.execute("""SELECT m.msg_uuid, c.title, m.role, m.created_at, m.conv_uuid,
                                  left(m.body, 240) FROM messages m
                           JOIN conversations c ON c.conv_uuid = m.conv_uuid
                           WHERE m.msg_uuid = ANY(%s)""", (top,))
            info = {r[0]: r for r in cur.fetchall()}
    lexset, semset = set(lex), set(sem)
    out = []
    for mid in top:
        d = _fmt(info[mid])
        d["rrf"] = round(scores[mid], 4)
        d["src"] = ("L" if mid in lexset else "") + ("S" if mid in semset else "")
        out.append(d)
    return out


_MODES = {"search": lexical, "semantic": semantic, "hybrid": hybrid}


@app.get("/api/{mode}")
def api(mode: str, q: str = Query(...), limit: int = Query(10, ge=1, le=50)):
    fn = _MODES.get(mode)
    if not fn:
        return JSONResponse({"error": "mode must be search | semantic | hybrid"}, status_code=400)
    return {"mode": mode, "query": q, "count": None, "results": fn(q, limit)}


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Chat Search</title>
<style>
  :root{--bg:#0d1117;--panel:#161b22;--bd:#30363d;--tx:#c9d3de;--dim:#8b949e;--hi:#e6edf3;--k:#79c0ff;--add:#7ee787;--now:#f0883e;
        --mono:"SFMono-Regular",Consolas,Menlo,monospace;--sans:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;}
  *{box-sizing:border-box}
  body{background:var(--bg);color:var(--tx);font-family:var(--sans);margin:0;padding:0 16px 60px;line-height:1.5}
  .wrap{max-width:780px;margin:0 auto}
  h1{font-size:20px;color:var(--hi);margin:26px 0 14px}
  .bar{display:flex;gap:8px;margin-bottom:6px}
  #q{flex:1;background:var(--panel);border:1px solid var(--bd);border-radius:8px;color:var(--hi);padding:11px 13px;font-size:15px;outline:none}
  #q:focus{border-color:var(--k)}
  .modes{display:flex;gap:6px;margin:8px 0 18px}
  .modes button{background:var(--panel);border:1px solid var(--bd);color:var(--dim);border-radius:20px;padding:5px 13px;font-size:12.5px;cursor:pointer}
  .modes button.on{color:var(--hi);border-color:var(--k);background:#10243c}
  .meta{color:var(--dim);font-size:12.5px;margin-bottom:10px}
  .hit{background:var(--panel);border:1px solid var(--bd);border-radius:10px;padding:12px 14px;margin:10px 0}
  .hit a.ttl{color:var(--hi);font-size:14.5px;text-decoration:none;font-weight:600}
  .hit a.ttl:hover{text-decoration:underline}
  .sub{color:var(--dim);font-size:12px;margin:2px 0 7px;font-family:var(--mono)}
  .snip{font-size:13.5px;color:var(--tx)}
  .tag{font-family:var(--mono);font-size:10px;padding:0 6px;border-radius:20px;border:1px solid var(--bd);color:var(--dim);margin-left:6px}
  .tag.LS{color:var(--add);border-color:#1f4a2f}.tag.S{color:var(--k)}.tag.L{color:var(--now)}
  mark{background:#3b2f15;color:#f0d090;padding:0 2px;border-radius:3px}
</style></head>
<body><div class="wrap">
  <h1>\U0001F50D Claude Chat Search</h1>
  <form class="bar" onsubmit="go(event)"><input id="q" placeholder="search your whole Claude history…" autofocus></form>
  <div class="modes">
    <button data-m="hybrid" class="on">Hybrid</button>
    <button data-m="search">Lexical</button>
    <button data-m="semantic">Semantic</button>
  </div>
  <div class="meta" id="meta"></div>
  <div id="out"></div>
</div>
<script>
let mode="hybrid";
document.querySelectorAll(".modes button").forEach(b=>b.onclick=()=>{
  mode=b.dataset.m;
  document.querySelectorAll(".modes button").forEach(x=>x.classList.toggle("on",x===b));
  if(document.getElementById("q").value.trim()) run();
});
function go(e){e.preventDefault();run();}
async function run(){
  const q=document.getElementById("q").value.trim(); if(!q)return;
  const meta=document.getElementById("meta"), out=document.getElementById("out");
  meta.textContent="searching…"; out.innerHTML="";
  try{
    const r=await fetch(`/api/${mode}?q=${encodeURIComponent(q)}&limit=15`);
    const d=await r.json();
    const res=d.results||[];
    meta.textContent=`${res.length} ${mode} result(s) for “${q}”`;
    out.innerHTML=res.map(h=>{
      const tag=h.src?`<span class="tag ${h.src}">${h.src}</span>`:"";
      const rrf=h.rrf!=null?` · rrf ${h.rrf}`:"";
      return `<div class="hit">
        <a class="ttl" href="https://claude.ai/chat/${h.conv_uuid}" target="_blank">${esc(h.title)}</a>${tag}
        <div class="sub">${h.created_at||""} · ${h.role}${rrf}</div>
        <div class="snip">${h.snippet}</div></div>`;
    }).join("")||`<div class="meta">no matches.</div>`;
  }catch(e){meta.textContent="error: "+e;}
}
function esc(s){const d=document.createElement("div");d.textContent=s;return d.innerHTML;}
</script>
</body></html>"""
