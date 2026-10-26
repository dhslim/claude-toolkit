---
name: dentalchart-be-deploy
description: "Deployment process, environment topology, and schema-change hazards for the dentalchart backend (Dentium-IT/dentalchart-backend). Load whenever deploying, tagging a release, adding a preview/review label, planning a merge to develop, or assessing whether a DB schema change is safe to roll out."
argument-hint: "[dev|preview|prod|schema-check]"
---

# dentalchart-be-deploy

Context for deploying `Dentium-IT/dentalchart-backend`. Source of truth is the repo `README.md`
(`## preview 서버 배포 프로세스`, `## dev 서버 배포 프로세스`); this skill adds the infra facts
and hazards that are **not** written down anywhere.

---

## Deploy order (the normal path)

```
1. PR 3-stage checklist  (see below)
2. merge the feature branch into  develop
3. tag the develop commit:  dev-<short sha>     (short sha = first 7 chars)
4. push the tag  →  cd-dev workflow fires automatically
5. wait for the 배포봇 notification in Slack  #전자차트-백엔드
6. post the 배포 공지  (format below)
```

**Merge first, then tag.** The CD workflow itself will build any commit a `dev-*` tag points at,
including an unmerged feature branch — but team practice is to tag develop commits, and the
README frames it as "개발 담당자가 merge/deploy 진행". Don't tag a feature branch without
saying so in the channel.

---

## PR stage checklist (README, `dev 서버 배포 프로세스`)

| 단계 | what | gate |
|---|---|---|
| `1단계` | PR 생성자 자가 테스트 | PR 작성 중 / 개발 담당자 테스트 중 / 테스트 케이스 작성 중 |
| `2단계` | 리뷰어 테스트 | **PR에 `review` label 추가** — 리뷰어의 코드 리뷰 및 테스트 케이스 검토 |
| `3단계` | FE 연동 검증 테스트 | develop 환경 배포 직전, 담당 FE 개발자에게 input/output 검증 요청 |

Check `gh pr view <n> --json labels` before assuming a stage has run. The `review` label is a
real gate and is easy to skip accidentally.

---

## Tag naming

```
SHA        9bd9a413cc263ad702e43d08e3e81b7dea0e5816
short sha  9bd9a41                (first 7 characters)
dev tag    dev-9bd9a41
prod tag   prod-<short sha>
```

```bash
git tag "dev-$(git rev-parse HEAD | cut -c1-7)"
git push origin "dev-$(git rev-parse HEAD | cut -c1-7)"
```

---

## 배포 공지 format

Post to Slack `#전자차트-백엔드` (`C05AC5KD3UH`) after 배포봇 confirms.

- **version**: the tag name verbatim (e.g. `dev-9bd9a41`)
- **업데이트 내용**: categorize each item as `feat` / `fix` / `deprecated`

---

## Environments

| env | trigger | replicas | POSTGRES_SYNCHRONIZE | notes |
|---|---|---|---|---|
| **local** | `npm run local` | — | `true` | |
| **preview** | add an unused `preview-N` label to the PR | — | `true` | re-pushing to the PR redeploys automatically; last push wins if two PRs share a label |
| **dev** | push tag `dev-*` | **1** | `true` | `kubectl set image deploy/dev` |
| **stage** | — | **2** | `true` | |
| **prod** | push tag `prod-*` | **6** | `true` | `kubectl set image deploy/core -n prod`, `maxSurge: 0`, `maxUnavailable: 1` |
| test | — | — | `false` | only env with sync off |

CD workflows: `.github/workflows/{preview,dev,prod}-cd.yml`.
Container TZ is `Asia/Seoul` (`api.Dockerfile` `ENV TZ`, plus the pod spec), and
`src/main.ts` calls `dayjs.tz.setDefault('Asia/Seoul')`.

Live logs (via 네이버 클라우드 원격 접속):
```bash
kubectl logs --follow deploy/dev
kubectl logs --follow deploy/preview-<n>
```

---

## ⚠️ Destructive schema changes — read before dropping a column

`POSTGRES_SYNCHRONIZE=true` in **every** deployed environment. `DatabaseModule` opens a
DataSource per tenant schema at boot (`src/modules/database/database.module.ts`, the
`forRootAsync` factory), so TypeORM runs schema sync — **including `ALTER TABLE … DROP COLUMN`**
— against every tenant schema during pod startup. Schemas named `preview*` are skipped; real
tenants are not. There is no migration Job, initContainer, or advisory lock gating this.

**Production is `replicas: 6` with `maxSurge: 0` / `maxUnavailable: 1`** — pods are replaced one
at a time. So on a destructive change:

1. new pod #1 boots and drops the column across all tenant schemas
2. **5 pods are still running the old image**, whose entity metadata still declares it
3. TypeORM emits explicit column lists (never `SELECT *`) → those pods raise Postgres `42703
   column … does not exist` on every affected query
4. this persists until all 6 pods are replaced

Additive changes are backward-compatible under this setup. **Destructive ones are not.**

### How to actually do it safely

**Textbook expand/contract does NOT work here — don't reach for it reflexively.** The obvious
split ("PR-A removes code references but keeps `@Column`; PR-B removes `@Column`") fails: as
long as `@Column` is declared, TypeORM builds that column into every generated SELECT and
INSERT. So when PR-B drops it, the still-running PR-A pods hit `42703` exactly as before. The
hazard moves from release 1 to release 2 rather than disappearing.

For expand/contract to work, release 1 would have to keep the column in the DB while removing
it from TypeORM's generated SQL (`@Column({ select: false })` gets partway, but INSERT
behaviour needs verifying). Fiddly — verify it on a preview server before betting a prod
release on it.

**The reliable options, given `synchronize: true` + rolling updates:**

| option | cost |
|---|---|
| scale `core` to 1 replica for the cutover, then back to 6 | brief reduced capacity, no overlap |
| temporarily set `strategy: Recreate` | brief full downtime during rollout |
| maintenance window | planned downtime |

All three trade a short *planned* interruption for avoiding several minutes of *unplanned* 500s
across 접수/진료비/수납 for every tenant. Pick one deliberately and announce it — do not let a
destructive change reach a `prod-*` tag without one.

Dev (`replicas: 1`) does not have this hazard — but deploying there **does** drop the column
from all tenant schemas of `chart_db_dev`, destroying that data in the shared dev database.

---

## Repo facts worth knowing

- **`*.spec.ts` files are deprecated** — the team does not maintain them. `tsconfig.build.json`
  excludes `**/*spec.ts`, and the `npm test` step in `.github/workflows/ci.yml` is commented out.
  Do not report findings in spec files, and do not treat "the build passes" as "the tests pass":
  it means `tsc` + `nest build` over non-spec sources only.
- **Verification that actually counts**: deploy to a `preview-N` server and exercise the real
  code path. Raw SQL in template literals is invisible to `tsc`, so a runtime check against a
  preview schema is the only way to validate it.
- **GraphQL contract changes break the FE hard.** A document selecting a removed field fails
  validation with `GRAPHQL_VALIDATION_FAILED` and returns *no data at all* — not a null field.
  Removed **input** fields fail the same way at input coercion. Both are caught on the FE side
  after codegen regenerates types and the build runs, but only if the FE regenerates. **FE
  deploys first** on any breaking schema change.
- The Redis response cache (`src/modules/cache`) keys on `schemaHash`, so a schema change
  orphans every stale entry rather than serving it. Not a concern on schema changes.

---

## Quick checks

```bash
# PR stage / labels
gh pr view <n> --repo Dentium-IT/dentalchart-backend --json labels,state,reviewDecision

# what a dev tag would be
echo "dev-$(git rev-parse HEAD | cut -c1-7)"

# recent deploys
git tag -l "dev-*" --sort=-creatordate | head -5
git tag -l "prod-*" --sort=-creatordate | head -5

# is this change destructive to the schema?
git diff origin/develop..HEAD -- 'src/**/*.entity.ts' | grep -E '^-\s*@Column'
```
