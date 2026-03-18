#!/usr/bin/env node
// SessionStart hook — 오늘 퀴즈를 안 풀었으면 Claude에게 퀴즈 지시
const fs = require('fs');
const path = require('path');

const TAKEN_FILE = path.join(__dirname, 'quiz-last-taken.txt');
const SHOWN_FILE = path.join(__dirname, 'quiz-last-shown.txt');
const today = new Date().toISOString().split('T')[0]; // YYYY-MM-DD

let lastTaken = '';
let lastShown = '';
try { lastTaken = fs.readFileSync(TAKEN_FILE, 'utf8').trim(); } catch {}
try { lastShown = fs.readFileSync(SHOWN_FILE, 'utf8').trim(); } catch {}

if (lastTaken === today || lastShown === today) {
  process.exit(0);
}

// 오늘 처음 보여줌 표시
fs.writeFileSync(SHOWN_FILE, today);

// 아직 안 풀었음 → Claude에게 지시 출력 (SessionStart stdout은 Claude context에 주입됨)
console.log(`[DAILY QUIZ] The user has not taken today's quiz yet.

IMPORTANT: Before doing ANY work the user asks for, you MUST run the daily quiz first.

Steps:
1. Launch a BACKGROUND agent to handle steps 2-4 below. Greet the user and tell them the quiz is being prepared while they can share what they want to work on.
2. (In background agent) Run: node C:/Users/user/scripts/conversation-warehouse/quiz-data.js
   This returns yesterday's conversation summaries from MongoDB.
3. (In background agent) Generate exactly 10 multiple-choice questions (4 choices each) based on that data.
   Focus on: concepts discussed, code patterns used, technical decisions made, bugs fixed.
4. (In background agent) IMMEDIATELY save the quiz to MongoDB by piping JSON to stdin:
   echo '{"questions":[{"q":"...","choices":["A)...","B)...","C)...","D)..."],"answer":"B"},...]}' | node C:/Users/user/scripts/conversation-warehouse/quiz-save.js
   The JSON must have a "questions" array where each item has "q", "choices", and "answer" fields.
   Multiple quizzes per day are allowed — each save creates a new document, never overwrites.
   Return the full list of questions and answers in the agent result.
5. When the background agent completes, present ALL 10 questions at once in a numbered list.
6. Wait for the user to answer.
7. Grade the answers and show explanations for wrong ones.
8. Run: node C:/Users/user/scripts/conversation-warehouse/quiz-mark-done.js
   This marks today's quiz as complete.

Keep it quick and fun. Do NOT skip the quiz.`);
