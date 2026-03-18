#!/usr/bin/env node
// Claude Code 대화 기록을 MongoDB Atlas에 동기화하는 스크립트
// 두 가지 모드: Hook 모드(stdin으로 단일 세션) / Cron 모드(전체 스캔)

const fs = require('fs');
const path = require('path');
const os = require('os');
const { MongoClient } = require('mongodb');

require('dotenv').config({ path: path.join(__dirname, '.env') });

const CLAUDE_PROJECTS_DIR = path.join(os.homedir(), '.claude', 'projects');
const DEVICE = os.hostname();

// 명시적으로 스킵할 타입만 필터링, 나머지는 전부 유지
const SKIP_TYPES = new Set([
  'file-history-snapshot', 'progress', 'last-prompt',
  'queue-operation'
]);

// 재시도 래퍼 — 일시적 네트워크 오류 대응
async function withRetry(fn, maxRetries = 3) {
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      return await fn();
    } catch (err) {
      const isTransient = err.message.includes('ENOTFOUND') ||
        err.message.includes('timed out') ||
        err.message.includes('ECONNRESET') ||
        err.message.includes('socket disconnected');
      if (!isTransient || attempt === maxRetries) throw err;
      // 지수 백오프: 1초, 2초, 4초
      await new Promise(r => setTimeout(r, 1000 * Math.pow(2, attempt - 1)));
    }
  }
}

// JSONL 파일 파싱 — 필터링된 메시지만 반환
function parseJSONL(filePath) {
  const content = fs.readFileSync(filePath, 'utf8');
  const lines = content.trim().split('\n').filter(Boolean);
  const rawLineCount = lines.length;

  let sessionId = null;
  let sessionName = null;
  let sessionDate = null;
  let project = null;
  const messages = [];

  for (const line of lines) {
    let obj;
    try {
      obj = JSON.parse(line);
    } catch {
      continue;
    }

    const type = obj.type;

    if (!sessionId && obj.sessionId) {
      sessionId = obj.sessionId;
    }
    if (obj.type === 'custom-title' && obj.customTitle) {
      sessionName = obj.customTitle;
    }
    if (!sessionDate && obj.timestamp) {
      sessionDate = new Date(obj.timestamp);
    }
    if (!project && obj.cwd) {
      project = obj.cwd;
    }

    if (SKIP_TYPES.has(type)) continue;

    const msg = obj.message;

    // local-command-caveat 시스템 래퍼 필터링 (AI에게 보내는 무시 지시문)
    if (type === 'user' && typeof msg?.content === 'string' && msg.content.includes('<local-command-caveat>')) {
      continue;
    }

    messages.push({
      type,
      role: msg?.role,
      content: msg?.content,
      timestamp: obj.timestamp,
      uuid: obj.uuid
    });
  }

  if (!sessionId) {
    sessionId = path.basename(filePath, '.jsonl');
  }
  if (!project) {
    project = path.basename(path.dirname(filePath));
  }

  return { sessionId, sessionName, project, sessionDate, rawLineCount, messages };
}

// JSONL 파일의 라인 수만 빠르게 카운트
function countLines(filePath) {
  const content = fs.readFileSync(filePath, 'utf8');
  return content.trim().split('\n').filter(Boolean).length;
}

// 모든 JSONL 파일 탐색
function findAllJSONLFiles() {
  const files = [];

  function walk(dir) {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(fullPath);
      } else if (entry.name.endsWith('.jsonl')) {
        files.push(fullPath);
      }
    }
  }

  walk(CLAUDE_PROJECTS_DIR);
  return files;
}

// MongoDB에 세션 upsert (재시도 포함)
async function upsertSession(collection, doc) {
  return withRetry(async () => {
    const result = await collection.updateOne(
      { session_id: doc.session_id },
      {
        $set: {
          session_name: doc.session_name,
          project: doc.project,
          device: doc.device,
          session_date: doc.session_date,
          synced_at: new Date(),
          message_count: doc.message_count,
          raw_line_count: doc.raw_line_count,
          messages: doc.messages
        }
      },
      { upsert: true }
    );
    return result.upsertedCount > 0 ? 'inserted' : (result.modifiedCount > 0 ? 'updated' : 'unchanged');
  });
}

// stdin에서 hook 데이터 읽기 시도
function readStdin() {
  return new Promise((resolve) => {
    // Windows에서는 fstatSync(0)이 TTY도 isFIFO()=false, isFile()=false로 반환
    // 안전하게: 무조건 읽기 시도, 타임아웃으로 빈 stdin 감지
    let data = '';
    let resolved = false;

    const done = (result) => {
      if (resolved) return;
      resolved = true;
      process.stdin.pause();
      process.stdin.removeAllListeners();
      resolve(result);
    };

    try {
      process.stdin.setEncoding('utf8');
      process.stdin.on('data', chunk => { data += chunk; });
      process.stdin.on('end', () => done(data.trim()));
      process.stdin.on('error', () => done(''));
      process.stdin.resume();

      // 500ms 타임아웃 — stdin이 비어있으면 cron 모드
      setTimeout(() => done(data.trim()), 500);
    } catch {
      done('');
    }
  });
}

// 단일 파일 동기화
async function syncOneFile(collection, filePath) {
  const parsed = parseJSONL(filePath);
  if (!parsed.sessionId || parsed.messages.length === 0) return null;

  const doc = {
    session_id: parsed.sessionId,
    session_name: parsed.sessionName || null,
    project: parsed.project,
    device: DEVICE,
    session_date: parsed.sessionDate || new Date(),
    message_count: parsed.messages.length,
    raw_line_count: parsed.rawLineCount,
    messages: parsed.messages
  };

  // BSON 16MB 제한 체크
  const estimatedSize = JSON.stringify(doc).length;
  if (estimatedSize > 14 * 1024 * 1024) {
    console.warn(`⚠ 세션 ${parsed.sessionId} 크기 초과 (${(estimatedSize / 1024 / 1024).toFixed(1)}MB) — 메시지 잘라냄`);
    while (JSON.stringify(doc).length > 14 * 1024 * 1024 && doc.messages.length > 10) {
      doc.messages = doc.messages.slice(0, Math.floor(doc.messages.length * 0.8));
      doc.message_count = doc.messages.length;
    }
  }

  const action = await upsertSession(collection, doc);
  return { sessionId: parsed.sessionId, action, messageCount: parsed.messages.length };
}

// 전체 스캔 (cron 모드)
async function syncAll(collection) {
  const db = collection.s.db;
  const cacheCol = db.collection('file_sync_cache');
  await cacheCol.createIndex({ file_path: 1 }, { unique: true });

  const files = findAllJSONLFiles();
  console.log(`📂 ${files.length}개 JSONL 파일 발견`);

  // 파일 경로별 라인 수 캐시 조회
  const existing = await cacheCol.find({}).toArray();
  const cacheMap = new Map(existing.map(e => [e.file_path, e.line_count]));

  let inserted = 0, updated = 0, skipped = 0, errors = 0;

  for (const filePath of files) {
    try {
      const normalizedPath = filePath.replace(/\\/g, '/');
      const currentLines = countLines(filePath);

      // 라인 수가 동일하면 스킵
      if (cacheMap.get(normalizedPath) === currentLines) {
        skipped++;
        continue;
      }

      const result = await syncOneFile(collection, filePath);
      if (result) {
        if (result.action === 'inserted') inserted++;
        else if (result.action === 'updated') updated++;
        else skipped++;

        // 캐시 갱신 (재시도 포함)
        await withRetry(() => cacheCol.updateOne(
          { file_path: normalizedPath },
          { $set: { file_path: normalizedPath, line_count: currentLines, synced_at: new Date() } },
          { upsert: true }
        ));
      }
    } catch (err) {
      errors++;
      console.error(`❌ ${path.basename(filePath)}: ${err.message}`);
    }
  }

  console.log(`✅ 완료: ${inserted} 삽입, ${updated} 갱신, ${skipped} 스킵, ${errors} 오류`);
}

// Hook 모드 — stdin에서 transcript_path 파싱
async function syncFromHook(collection, stdinData) {
  let hookData;
  try {
    hookData = JSON.parse(stdinData);
  } catch {
    console.error('stdin JSON 파싱 실패, cron 모드로 전환');
    return syncAll(collection);
  }

  const transcriptPath = hookData.transcript_path || hookData.session_id;
  if (!transcriptPath) {
    console.log('transcript_path 없음, 전체 스캔 실행');
    return syncAll(collection);
  }

  let filePath = transcriptPath;
  if (!fs.existsSync(filePath)) {
    const files = findAllJSONLFiles();
    const match = files.find(f => path.basename(f, '.jsonl') === transcriptPath);
    if (!match) {
      console.error(`파일을 찾을 수 없음: ${transcriptPath}`);
      return;
    }
    filePath = match;
  }

  const result = await syncOneFile(collection, filePath);
  if (result) {
    console.log(`✅ ${result.action}: ${result.sessionId} (${result.messageCount}개 메시지)`);
  }
}

// CLI 인자 파싱
function parseArgs() {
  const args = process.argv.slice(2);
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--file' && args[i + 1]) return { mode: 'file', filePath: args[i + 1] };
    if (args[i] === '--scan') return { mode: 'scan' };
  }
  return { mode: 'stdin' }; // 기본값: stdin에서 hook 데이터 읽기 시도
}

// 메인 실행
async function main() {
  const uri = process.env.MONGODB_URI;
  if (!uri) {
    console.error('MONGODB_URI 환경변수가 설정되지 않음');
    process.exit(1);
  }

  const client = new MongoClient(uri, {
    serverSelectionTimeoutMS: 10000,
    connectTimeoutMS: 10000,
    socketTimeoutMS: 30000,
    retryWrites: true,
    retryReads: true
  });

  try {
    await client.connect();
    const db = client.db('conversation-warehouse');
    const collection = db.collection('sessions');

    await collection.createIndex({ session_id: 1 }, { unique: true });
    await collection.createIndex({ session_date: 1 });

    const { mode, filePath } = parseArgs();

    if (mode === 'file' && filePath) {
      // --file 모드: 특정 파일만 동기화 (가장 가벼움)
      const resolved = path.resolve(filePath);
      if (!fs.existsSync(resolved)) {
        console.error(`파일 없음: ${resolved}`);
        process.exit(1);
      }
      const result = await syncOneFile(collection, resolved);
      if (result) console.log(`✅ ${result.action}: ${result.sessionId} (${result.messageCount}개 메시지)`);
    } else if (mode === 'scan') {
      // --scan 모드: 전체 스캔 (cron용)
      await syncAll(collection);
    } else {
      // stdin 모드: hook에서 JSON 읽기
      const stdinData = await readStdin();
      if (stdinData) {
        await syncFromHook(collection, stdinData);
      } else {
        await syncAll(collection);
      }
    }
  } catch (err) {
    console.error('동기화 실패:', err.message);
    process.exit(1);
  } finally {
    await client.close();
  }
}

main();
