---
name: review-quiz
description: Re-read today's daily quiz in full and go over any question
disable-model-invocation: true
---

# Quiz Review

```
{{VENV_PYTHON}} {{SCRIPT_DIR}}/quiz_review.py
```

Returns today's quiz. Every question carries `question`, the full `choices` array,
`correct_answer`, `user_answer`, and `correct` — **for every item, right or wrong**.

## Rendering contract

**Always restate the question in full, with all four choices verbatim, before saying
anything about it.** Applies to every question: right ones, wrong ones, ones asked
about by number.

Never refer to a question by number alone, paraphrase it, or drop choice text. The
user should never have to scroll back. Long output is fine — completeness wins here.

Format per question:

```
**N.** <question verbatim>

A) <verbatim> · B) <verbatim> · C) <verbatim> · D) <verbatim>

Your answer: X · Correct: Y   (✅ / ❌)
<why the correct choice is right; if wrong, why the user's pick fails>
```

For correct answers, still restate everything, then say what made the strongest
distractor tempting.

If `duplicates_today` is greater than 1, mention it — more than one quiz was saved
for today and this is the graded one.
