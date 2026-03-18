#!/usr/bin/env node
// MongoDB에서 어제의 대화 요약을 가져옴 — Claude가 퀴즈 생성에 사용
const path = require('path');
const { MongoClient } = require('mongodb');
require('dotenv').config({ path: path.join(__dirname, '.env') });

async function main() {
  // 어제 날짜 범위
  const now = new Date();
  const localYesterday = new Date(now);
  localYesterday.setDate(localYesterday.getDate() - 1);
  const yStr = `${localYesterday.getFullYear()}-${String(localYesterday.getMonth() + 1).padStart(2, '0')}-${String(localYesterday.getDate()).padStart(2, '0')}`;
  const start = new Date(yStr + 'T00:00:00.000Z');
  const end = new Date(yStr + 'T23:59:59.999Z');

  const client = new MongoClient(process.env.MONGODB_URI, {
    serverSelectionTimeoutMS: 5000
  });

  try {
    await client.connect();
    const db = client.db('conversation-warehouse');
    const sessions = await db.collection('sessions').find({
      session_date: { $gte: start, $lte: end }
    }).toArray();

    if (sessions.length === 0) {
      // 어제 세션이 없으면 최근 3일 확장
      const threeDaysAgo = new Date(now);
      threeDaysAgo.setDate(threeDaysAgo.getDate() - 3);
      // Use local midnight as lower bound
      const tdStr = `${threeDaysAgo.getFullYear()}-${String(threeDaysAgo.getMonth() + 1).padStart(2, '0')}-${String(threeDaysAgo.getDate()).padStart(2, '0')}`;
      const threeDaysAgoStart = new Date(tdStr + 'T00:00:00.000Z');
      const recent = await db.collection('sessions').find({
        session_date: { $gte: threeDaysAgoStart, $lte: end }
      }).sort({ session_date: -1 }).limit(5).toArray();

      if (recent.length === 0) {
        console.log('No recent sessions found for quiz generation.');
        process.exit(0);
      }
      sessions.push(...recent);
    }

    // 대화 요약 추출 (user/assistant text만, 50K 문자 제한)
    const chunks = [];
    let totalChars = 0;
    const MAX_CHARS = 50000;

    for (const session of sessions) {
      if (totalChars >= MAX_CHARS) break;
      chunks.push(`\n--- Session: ${session.project} (${session.session_date?.toISOString()?.split('T')[0]}) ---\n`);

      for (const msg of session.messages || []) {
        if (totalChars >= MAX_CHARS) break;
        if (msg.role !== 'user' && msg.role !== 'assistant') continue;

        let text = '';
        if (typeof msg.content === 'string') {
          text = msg.content;
        } else if (Array.isArray(msg.content)) {
          text = msg.content.filter(b => b.type === 'text').map(b => b.text).join('\n');
        }

        if (text.length > 0) {
          const prefix = msg.role === 'user' ? 'User' : 'Claude';
          const truncated = text.slice(0, 1000);
          chunks.push(`[${prefix}]: ${truncated}`);
          totalChars += truncated.length;
        }
      }
    }

    console.log(`Found ${sessions.length} sessions from yesterday.`);
    console.log(chunks.join('\n'));

  } catch (err) {
    console.error('Failed to fetch quiz data:', err.message);
    process.exit(1);
  } finally {
    await client.close();
  }
}

main();
