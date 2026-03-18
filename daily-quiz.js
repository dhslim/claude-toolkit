#!/usr/bin/env node
// 일일 퀴즈 생성 — MongoDB의 대화 기록에서 복습 문제를 자동 생성
// 사용법: node daily-quiz.js [--date 2026-03-17]

const path = require('path');
const { MongoClient } = require('mongodb');
const Anthropic = require('@anthropic-ai/sdk');

require('dotenv').config({ path: path.join(__dirname, '.env') });

// CLI 인자 파싱
function parseArgs() {
  const args = process.argv.slice(2);
  let date = null;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--date' && args[i + 1]) {
      date = args[i + 1];
      i++;
    }
  }

  // 기본값: 어제
  if (!date) {
    const yesterday = new Date();
    yesterday.setDate(yesterday.getDate() - 1);
    date = yesterday.toISOString().split('T')[0];
  }

  return { date };
}

// 세션에서 퀴즈용 컨텍스트 추출
function extractContent(sessions) {
  const chunks = [];
  let totalChars = 0;
  const MAX_CHARS = 200000; // ~50K 토큰

  for (const session of sessions) {
    if (totalChars >= MAX_CHARS) break;

    chunks.push(`\n--- 세션: ${session.project} (${session.session_date?.toISOString()?.split('T')[0]}) ---\n`);

    for (const msg of session.messages || []) {
      if (totalChars >= MAX_CHARS) break;

      if (msg.role === 'user' || msg.role === 'assistant') {
        let text = '';
        if (typeof msg.content === 'string') {
          text = msg.content;
        } else if (Array.isArray(msg.content)) {
          text = msg.content
            .filter(b => b.type === 'text')
            .map(b => b.text)
            .join('\n');
        }

        if (text.length > 0) {
          const prefix = msg.role === 'user' ? '사용자' : 'Claude';
          const truncated = text.slice(0, 2000);
          chunks.push(`[${prefix}]: ${truncated}`);
          totalChars += truncated.length + 20;
        }
      }
    }
  }

  return chunks.join('\n');
}

// Claude API로 퀴즈 생성
async function generateQuiz(anthropic, content, date) {
  const response = await anthropic.messages.create({
    model: 'claude-sonnet-4-6',
    max_tokens: 4096,
    messages: [{
      role: 'user',
      content: `다음은 ${date}에 AI 코딩 어시스턴트와 나눈 대화 기록입니다. 이 대화에서 다룬 개념, 코드 패턴, 기술적 결정, 버그 수정 등을 기반으로 복습 퀴즈를 만들어주세요.

규칙:
1. 정확히 10개의 4지선다 문제를 만들어주세요
2. 실제 대화에서 다룬 내용만 기반으로 출제
3. "바람직한 어려움(desirable difficulty)" 원칙 적용 — 단순 암기가 아닌 이해도를 테스트
4. 각 문제에 정답과 설명 포함
5. JSON 배열로 반환 (다른 텍스트 없이 순수 JSON만)

JSON 형식:
[{
  "question": "문제 텍스트",
  "choices": ["A) ...", "B) ...", "C) ...", "D) ..."],
  "answer": "A",
  "explanation": "설명",
  "topic": "주제 키워드"
}]

대화 기록:
${content}`
    }]
  });

  const text = response.content[0].text;

  // JSON 추출 (코드블록 안에 있을 수 있음)
  const jsonMatch = text.match(/\[[\s\S]*\]/);
  if (!jsonMatch) {
    throw new Error('Claude 응답에서 JSON을 찾을 수 없음');
  }

  return JSON.parse(jsonMatch[0]);
}

async function main() {
  const { date } = parseArgs();
  console.log(`📝 ${date} 대화 기반 퀴즈 생성 시작`);

  const mongoClient = new MongoClient(process.env.MONGODB_URI, {
    serverSelectionTimeoutMS: 5000
  });

  try {
    await mongoClient.connect();
    const db = mongoClient.db('conversation-warehouse');

    // 해당 날짜의 세션 조회
    const startOfDay = new Date(date + 'T00:00:00.000Z');
    const endOfDay = new Date(date + 'T23:59:59.999Z');

    const sessions = await db.collection('sessions').find({
      session_date: { $gte: startOfDay, $lte: endOfDay }
    }).toArray();

    if (sessions.length === 0) {
      console.log(`⚠ ${date}에 해당하는 세션이 없습니다`);
      process.exit(0);
    }

    console.log(`📂 ${sessions.length}개 세션 발견`);

    // 컨텍스트 추출
    const content = extractContent(sessions);
    if (content.length < 100) {
      console.log('⚠ 대화 내용이 너무 짧아 퀴즈 생성 불가');
      process.exit(0);
    }

    console.log(`📄 ${(content.length / 1000).toFixed(1)}K 문자 추출`);

    // Claude API로 퀴즈 생성
    const anthropic = new Anthropic();
    const questions = await generateQuiz(anthropic, content, date);

    console.log(`✅ ${questions.length}개 문제 생성 완료`);

    // MongoDB에 저장
    const quizDoc = {
      date,
      generated_at: new Date(),
      session_count: sessions.length,
      question_count: questions.length,
      questions
    };

    await db.collection('daily-quizzes').updateOne(
      { date },
      { $set: quizDoc },
      { upsert: true }
    );

    console.log(`💾 quiz_questions 컬렉션에 저장 완료`);

    // 미리보기 출력
    questions.forEach((q, i) => {
      console.log(`\n${i + 1}. [${q.topic}] ${q.question}`);
      q.choices.forEach(c => console.log(`   ${c}`));
      console.log(`   → 정답: ${q.answer}`);
    });

  } catch (err) {
    console.error('퀴즈 생성 실패:', err.message);
    process.exit(1);
  } finally {
    await mongoClient.close();
  }
}

main();
