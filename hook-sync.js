#!/usr/bin/env node
// Hook 래퍼 — stdin에서 transcript_path 추출 후 단일 파일만 동기화
// Stop/SessionEnd 훅에서 호출됨
// 핵심: 자식 프로세스를 detach하여 부모(hook)가 죽어도 동기화 완료

const fs = require('fs');
const path = require('path');

const LOG_FILE = path.join(__dirname, 'sync.log');

function log(msg) {
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19);
  fs.appendFileSync(LOG_FILE, `[${ts}] ${msg}\n`);
}

let data = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { data += chunk; });
process.stdin.on('end', () => {
  try {
    const hook = JSON.parse(data.trim());
    const tp = hook.transcript_path;
    const event = hook.hook_event_name || 'unknown';
    const sid = hook.session_id?.slice(0, 8) || '?';
    if (tp) {
      const { spawn } = require('child_process');
      const logFd = fs.openSync(LOG_FILE, 'a');
      const child = spawn('node', [
        path.join(__dirname, 'sync-conversations.js'),
        '--file', tp
      ], {
        detached: true,
        stdio: ['ignore', logFd, logFd],
        windowsHide: true
      });
      child.unref();
      log(`${event} → ${sid} sync started`);
    }
  } catch (err) {
    log(`ERROR: ${err.message}`);
  }
});
process.stdin.resume();
