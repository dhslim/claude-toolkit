#!/usr/bin/env node
// 오늘 퀴즈 완료 마커 기록
const fs = require('fs');
const path = require('path');

const MARKER_FILE = path.join(__dirname, 'quiz-last-taken.txt');
const now = new Date();
const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
fs.writeFileSync(MARKER_FILE, today);
console.log(`Quiz marked as done for ${today}`);
