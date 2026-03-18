#!/usr/bin/env node
// 오늘 퀴즈 완료 마커 기록
const fs = require('fs');
const path = require('path');

const MARKER_FILE = path.join(__dirname, 'quiz-last-taken.txt');
const today = new Date().toISOString().split('T')[0];
fs.writeFileSync(MARKER_FILE, today);
console.log(`Quiz marked as done for ${today}`);
