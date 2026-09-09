# Claude Proxy — 왜 필요한가 (사람용 설명)

> 이 문서는 `claude_proxy.py` 가 우리 long-session productivity 의 핵심 인프라인
> 이유를 사람 (그리고 Claude) 이 빠르게 이해할 수 있도록 정리한 explainer 입니다.
> 깊은 설계/운영 디테일은 [claude-proxy-design.md](./claude-proxy-design.md) 와
> [claude-proxy-operations.md](./claude-proxy-operations.md) 참고.

## 한 줄

**Proxy 가 우리 context engineering 의 핵심 layer**. CC ↔ Anthropic 사이에 끼어서
sliding-window trim (sawtooth), 캐시 최적화, 그리고 CC 버그 자동 교정을 한다.

## Proxy 가 하는 일

```
CC (claude.exe)
  ↓ HTTP POST /v1/messages
  ↓ body=525k tokens 통째로

[claude_proxy.py @ localhost:9999]   ← 우리 proxy
  ① Parse request body
  ② [1m] model rewrite (CC 2.1.145 bug fix)
  ③ Sliding-window trim (sawtooth)
     - 토큰 카운트 (count_tokens API)
     - threshold (700k) 넘으면 anchor 앞으로 0.5 이동
     - 옛 messages drop, 뒷부분만 유지
  ④ Cache control rewrite (cache_read 최대화)
  ⑤ Logging (proxy.log 에 매 요청 기록)
  ↓ HTTP forward

Anthropic API (api.anthropic.com)
  ↓ 정상 size 의 request 받음
  ↓ Opus 4.7 처리
  ↓ Streaming response

[claude_proxy.py]
  ⑥ Response stream-through (그대로 전달)
  ⑦ Token usage 기록

CC
  ↓ 사용자에게 표시
```

## 기능별 분류

### Context engineering

- **Sawtooth trim** — 700k 임계 도달 시 뒷 절반만 유지. 매번 임계 도달하면 또 자름.
  결과적으로 토큰 사용량 그래프가 톱니바퀴 패턴 (sawtooth).
- **Anchor sliding** — 매 trim 시 어디부터 keep 할지 결정. 캐시 hit 률 유지하려고
  의도적으로 같은 anchor 위치를 여러 turn 유지.
- **Cache 최적화** — Anthropic 의 prompt cache 가 prefix 매칭 기반. trim 위치를
  신중히 선택해서 cache_read 유지.

### Bug workaround

- **`[1m]` model rewrite** — CC 2.1.145 가 `model="claude-opus-4-7[1m]"` 라는
  잘못된 형식으로 probe 를 보냄 (Anthropic 은 그런 모델 모름 → 404). Proxy 가
  intercept 해서 `model="claude-opus-4-7"` + `anthropic-beta: context-1m-2025-08-07`
  헤더로 변환 후 forward. CC 가 "Context limit reached" 거짓 메시지 안 뜨게 됨.

### Observability

- **`proxy.log`** — 매 요청 wire-level 기록 (cache_read, body size, model, 응답 시간 등).
  토큰 어디서 새는지 추적 가능.
- **Token 카운트** — Anthropic 이 실제로 받는 토큰 수 (CC 가 보고하는 수가 아님).

## 효과 비교

| | 없으면 | 있으면 |
|---|---|---|
| Context 한도 도달 | 200k 도달 시 `/compact` 강제 (lossy) 또는 `/clear` (전체 손실) | 영원히 갈 수 있음 (옛 message drop 하며 진행) |
| 토큰 비용 | CC 가 700k+ 보내면 매 turn 비싸짐 | 매 turn 350-400k 만 forward (cache_read 99% hit) |
| CC 버그 (`[1m]` probe) | 첫 prompt 가 "Context limit reached" 로 실패. retry 필요 | 자동 교정. 첫 시도 성공 |
| Wire 디버깅 | 토큰 어디서 새는지 모름 (blackbox) | proxy.log 로 매 byte 추적 가능 |

## CC 입장에선 invisible

CC 는 자기가 prompt 통째로 Anthropic 에 보낸다고 생각.
실제론 proxy 가 똑똑하게 trim/rewrite/forward.
CC 는 자기 응답을 받을 뿐 proxy 가 끼어있는지도 모름.

→ 사용자 경험: 끊임없는 긴 세션 가능 + 비용 절감 + CC 버그 자동 patch.

## 활성화

```powershell
# PowerShell 에서:
$env:ANTHROPIC_BASE_URL = "http://localhost:9999"

# proxy 시작 (별도 터미널, persistent):
cd C:\Users\user\Desktop\claude-toolkit
.venv\Scripts\python.exe claude_proxy.py

# CC 새로 시작 (env var 가 이미 적용되어야 함)
claude
```

`ANTHROPIC_BASE_URL` 이 set 안 됐거나 proxy 가 안 돌고 있으면 CC 는 그냥
Anthropic 에 직접 연결 — proxy 의 이득 0.

## 검증

Proxy 가 실제로 일하고 있는지 확인:

```bash
# 1. Port 9999 listening?
netstat -an | grep 9999 | grep LISTENING

# 2. 최근 요청 처리됨?
tail -20 C:/Users/user/Desktop/claude-toolkit/proxy.log

# 3. Sliding-window 발동?
grep "sliding-window" C:/Users/user/Desktop/claude-toolkit/proxy.log | tail -5

# 4. [1m] rewrite 발동? (CC 새 세션 시작 후)
grep "rewrote model" C:/Users/user/Desktop/claude-toolkit/proxy.log | tail -3
```

## 한 줄로 다시

**이게 없으면 200k 정도에서 자주 `/compact` 또는 `/clear` 강제, 매 turn 비용 ↑,
CC 버그 직격타.**
**Proxy 가 long-session productivity 의 핵심 인프라.**
