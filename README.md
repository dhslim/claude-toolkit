# Conversation Warehouse

Claude Code 대화 기록을 MongoDB Atlas에 자동 동기화하고, 매일 퀴즈를 생성하는 시스템.

## 구조

```
~/scripts/conversation-warehouse/
├── sync-conversations.js    # 핵심 동기화 스크립트 (--file, --scan 모드)
├── hook-sync.js             # Stop/SessionEnd hook 래퍼 (detached process)
├── quiz-check.js            # SessionStart hook — 오늘 퀴즈 여부 확인
├── quiz-data.js             # MongoDB에서 어제 대화 요약 가져오기
├── quiz-mark-done.js        # 퀴즈 완료 마커 기록
├── dump-stdin.js            # (TODO: 안정화 후 제거) 디버그용 stdin 덤프
├── sync.log                 # hook 실행 로그
├── quiz-last-taken.txt      # 마지막 퀴즈 완료 날짜
├── last-hook-stdin.json     # 마지막 SessionStart stdin 덤프
├── .env                     # MongoDB URI
└── package.json             # mongodb, dotenv
```

## 동기화 흐름

3개의 hook이 모든 케이스를 커버:

| Hook | 트리거 | 동작 | 커버하는 케이스 |
|------|--------|------|----------------|
| **Stop** | Claude 응답 완료 후 | 현재 세션 1개 동기화 | 정상 흐름 (99%) |
| **SessionEnd** | `/exit` 또는 터미널 종료 | 현재 세션 1개 동기화 | 중단 후 종료 |
| **SessionStart** | 모든 Claude 시작 | 전체 스캔 (`--scan`) | 강제 종료로 놓친 세션 |

cron 불필요 — SessionStart 전체 스캔이 대체.

### 왜 cron이 필요 없는가

- Stop → 매 응답마다 동기화
- SessionEnd → 종료 시 동기화
- 강제 종료 → 다음 Claude 시작 시 SessionStart가 전체 스캔으로 보완
- 유일한 갭: 강제 종료 후 다시 Claude를 안 여는 경우 (무시 가능)

## Hook 설정 (`~/.claude/settings.json`)

```json
{
  "hooks": {
    "Stop": [{ "matcher": "", "hooks": [{
      "type": "command",
      "command": "node C:/Users/user/scripts/conversation-warehouse/hook-sync.js",
      "async": true, "timeout": 30000
    }]}],
    "SessionEnd": [{ "matcher": "", "hooks": [{
      "type": "command",
      "command": "node C:/Users/user/scripts/conversation-warehouse/hook-sync.js",
      "timeout": 10000
    }]}],
    "SessionStart": [{ "matcher": "", "hooks": [
      { "type": "command",
        "command": "node .../sync-conversations.js --scan",
        "async": true, "timeout": 60000 },
      { "type": "command",
        "command": "node .../dump-stdin.js",
        "async": true, "timeout": 5000 },
      { "type": "command",
        "command": "node .../quiz-check.js",
        "timeout": 3000 }
    ]}]
  }
}
```

## 핵심 설계 결정

### SessionEnd "Hook cancelled" 해결
- 문제: SessionEnd 기본 타임아웃 1.5초, MongoDB 연결 ~2.4초
- 해결: `hook-sync.js`가 detached 프로세스를 spawn → 즉시 종료 → 자식이 백그라운드에서 동기화

### 변경 감지 (idempotency)
- `file_sync_cache` 컬렉션에 파일 경로별 라인 수 저장
- 라인 수 동일 → 스킵 (파싱 없이)

### 16MB BSON 제한
- SKIP_TYPES 블랙리스트만 필터링 (file-history-snapshot, progress, last-prompt, queue-operation)
- 나머지 전부 저장 (tool_use input, tool_result output, thinking 블록 포함 — verbose 모드와 동일)
- local-command-caveat 시스템 래퍼만 추가 필터링
- 14MB 초과 시 자동 잘라냄

### claude -c 중복 없음
- `claude -c` / `claude --resume` → 같은 JSONL 파일에 append (같은 session_id)
- 별도 파일 생성 안 됨 → 중복 저장 문제 없음

## 일일 퀴즈

### 흐름
1. SessionStart 시 `quiz-check.js` 실행
2. `quiz-last-taken.txt`에 오늘 날짜 없으면 → Claude에게 퀴즈 지시 주입
3. Claude가 `quiz-data.js`로 어제 대화 가져와서 10문제 생성
4. 사용자 답변 후 `quiz-mark-done.js`로 완료 표시
5. 같은 날 재시작 시 퀴즈 스킵

### 특징
- Anthropic API 키 불필요 — Claude Code 자체가 퀴즈 생성
- 사용자 차단 없음 — 백그라운드 에이전트로 준비
- 하루 1회만 — 마커 파일로 제어

## MongoDB 스키마

### sessions 컬렉션
```js
{
  session_id: "uuid",           // unique index
  session_name: "stt-architecture",  // /rename으로 설정한 세션 이름 (JSONL의 custom-title에서 추출)
  project: "C:\\Users\\...",
  device: "DESKTOP-0DR960U",
  session_date: ISODate,        // index
  synced_at: ISODate,
  message_count: Number,
  raw_line_count: Number,
  messages: [{ type, role, content, timestamp, uuid }]
}
```

### file_sync_cache 컬렉션
```js
{
  file_path: "/normalized/path.jsonl",  // unique index
  line_count: Number,
  synced_at: ISODate
}
```

## TODO

- [ ] **퀴즈 데이터 신선도 문제**: quiz-data.js가 MongoDB에서 어제 세션을 가져오지만, 세션 데이터가 코드 변경 이전의 스냅샷일 수 있음. 예: "sync script가 tool_use를 필터링한다"는 정답이 어제는 맞았지만 오늘 변경됨 → 퀴즈가 구식 답을 정답으로 출제. 해결 방안: 퀴즈 문제가 "당시 코드 동작"이 아닌 "개념/이유"에 집중하도록 프롬프트 개선, 또는 quiz-data.js가 최신 동기화 후 데이터를 사용하도록 보장.

## 디버깅

```bash
# sync 로그 확인
tail -20 ~/scripts/conversation-warehouse/sync.log

# MongoDB 세션 수 확인
cd ~/scripts/conversation-warehouse && node -e "
require('dotenv').config();
const {MongoClient}=require('mongodb');
(async()=>{const c=new MongoClient(process.env.MONGODB_URI);
await c.connect();
console.log(await c.db('conversation-warehouse').collection('sessions').countDocuments());
await c.close()})();"

# 특정 세션에서 텍스트 검색
cd ~/scripts/conversation-warehouse && node -e "
require('dotenv').config();
const {MongoClient}=require('mongodb');
(async()=>{const c=new MongoClient(process.env.MONGODB_URI);
await c.connect();
const s=await c.db('conversation-warehouse').collection('sessions').findOne({session_id:'SESSION_ID'});
s.messages.filter(m=>{
  const t=typeof m.content==='string'?m.content:
    Array.isArray(m.content)?m.content.filter(b=>b.type==='text').map(b=>b.text).join(' '):'';
  return t.includes('SEARCH_TERM');
}).forEach(m=>console.log(m.role,m.timestamp,JSON.stringify(m.content).slice(0,200)));
await c.close()})();"

# 마지막 SessionStart stdin 확인
cat ~/scripts/conversation-warehouse/last-hook-stdin.json | node -pe "JSON.stringify(JSON.parse(require('fs').readFileSync(0,'utf8')),null,2)"

# 수동 전체 동기화
cd ~/scripts/conversation-warehouse && node sync-conversations.js --scan
```
