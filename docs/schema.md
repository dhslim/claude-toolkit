# MongoDB Schema


### Timestamp convention

Every UTC datetime field has a companion `*_kst` field — an **ISO-8601 string** with explicit `+09:00` offset (e.g. `"2026-05-19T05:53:47.256+09:00"`). The UTC datetime is canonical for queries/sorting/index; the KST string is for unambiguous human reading when scanning docs. KST is stored as a *string* (not a fake-UTC datetime) so it can't be silently coerced by BSON's UTC-normalization. Naming uses the existing `_at` family (`session_started_at`, `last_synced_at`, etc.).

### sessions collection
```js
{
  session_id: "uuid",                  // unique index
  session_name: "stt-architecture",    // from /rename (JSONL custom-title)
  project: "/home/user/myproject",     // cwd from session
  device: "hostname",
  session_started_at: ISODate,         // index (was: session_date)
  session_started_at_kst: "...+09:00", // KST companion string
  last_synced_at: ISODate,             // (was: synced_at)
  last_synced_at_kst: "...+09:00",
  message_count: Number,
  raw_line_count: Number,
  messages: [{
    type, role, content,
    timestamp: "ISO-8601 UTC string",
    timestamp_kst: "ISO-8601 KST string",
    uuid, parentUuid
  }]
}
```

### file_sync_cache collection
```js
{
  file_path: "/normalized/path.jsonl", // unique index
  line_count: Number,
  last_synced_at: ISODate,             // (was: synced_at)
  last_synced_at_kst: "...+09:00"
}
```

### quiz-markers collection
```js
{
  date: "2026-03-19",                   // unique index (KST date)
  taken_at: ISODate | null,             // when quiz was completed
  taken_at_kst: "...+09:00" | null,
  dismissed_at: ISODate | null,         // when quiz was dismissed
  dismissed_at_kst: "...+09:00" | null
}
```

### daily-quizzes collection
```js
{
  date: "2026-03-19",                   // KST date
  created_at: ISODate,                  // quiz generated
  created_at_kst: "...+09:00",
  questions: [{ q, choices, answer }],
  answers: ["A", "B", ...] | null,      // user's answers (after grade)
  score: Number | null,
  total: Number | null,
  graded: Boolean,
  graded_at: ISODate | null,
  graded_at_kst: "...+09:00" | null
}
```

