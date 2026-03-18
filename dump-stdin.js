#!/usr/bin/env node
// TODO: 시스템 안정화 후 제거 (settings.json SessionStart hook에서도 제거)
// 디버그용 — hook stdin을 파일에 저장
const fs = require('fs');
const path = require('path');
let d = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', c => d += c);
process.stdin.on('end', () => {
  fs.writeFileSync(path.join(__dirname, 'last-hook-stdin.json'), d);
});
process.stdin.resume();
