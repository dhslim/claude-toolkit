#!/usr/bin/env node
// 생성된 퀴즈를 MongoDB에 저장 — stdin으로 JSON 받음
const path = require('path');
const { MongoClient } = require('mongodb');
require('dotenv').config({ path: path.join(__dirname, '.env') });

async function main() {
  // stdin에서 JSON 읽기
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  const input = Buffer.concat(chunks).toString('utf8').trim();

  if (!input) {
    console.error('No quiz data provided on stdin.');
    process.exit(1);
  }

  let quizData;
  try {
    quizData = JSON.parse(input);
  } catch (e) {
    console.error('Invalid JSON:', e.message);
    process.exit(1);
  }

  const client = new MongoClient(process.env.MONGODB_URI, {
    serverSelectionTimeoutMS: 5000
  });

  try {
    await client.connect();
    const db = client.db('conversation-warehouse');
    const doc = {
      date: (() => { const n = new Date(); return `${n.getFullYear()}-${String(n.getMonth() + 1).padStart(2, '0')}-${String(n.getDate()).padStart(2, '0')}`; })(),
      created_at: new Date(),
      questions: quizData.questions,
      answers: quizData.answers || null,
      score: quizData.score || null,
      graded: false
    };

    const result = await db.collection('daily-quizzes').insertOne(doc);
    console.log(`Quiz saved: ${result.insertedId}`);
  } catch (err) {
    console.error('Failed to save quiz:', err.message);
    process.exit(1);
  } finally {
    await client.close();
  }
}

main();
