---
name: writing-teirei-minutes
description: Use when asked to assemble/まとめる the weekly チーム内定例 資料 (team internal weekly meeting minutes) in this docs repo — e.g. "7/7〜の進捗をチーム内定例でまとめて", update the 議事録 for this week's teirei, or produce the case-by-case progress doc. Covers gathering report-period updates from esa posts and GitHub, the standard section layout, the `YYYY_MM_DD_チーム内定例` title rule, publishing/patching the doc via the esa API, and syncing docs/ back down with `abist-kb sync batch`.
---

# Writing チーム内定例 Minutes

## Overview

The weekly **チーム内定例** doc is a **diff-based, case-by-case progress report**: for each 恒常案件 (standing project), it records only what changed since last week's teirei, cites the source esa 記事 / GitHub, and lists per-owner follow-ups. **Reuse last week's file as the structural template** and update the deltas — do not invent a new layout.

**Core principle:** report the *delta* (先週報告済みは省略), cite every claim to an esa `#NNNN` or GitHub PR/issue, and keep language plain enough for non-specialist teammates.

## Where the file lives

```
docs/esa/設計効率化/議事録/設計効率化/チーム内定例/YYYY_MM_DD_チーム内定例.md
```
- Teirei is usually **Tuesday** (8/24 was a Monday exception). New file's date = the meeting date. Report period = **前回teirei日 〜 今回teirei日**.
- The path really does repeat `設計効率化` twice — `docs/esa/<batch>/<esa category>` where the category is `議事録/設計効率化/チーム内定例`. Don't "fix" it.
- New unpublished file: leave frontmatter `post_number:` / `url:` **empty** (esa assigns on publish) and `managed_by: human`. Set `date`/`updated_at` to the meeting date.

### Title rule (esa 記事名 = ファイル名)

**The title MUST carry the meeting date, as `YYYY_MM_DD_チーム内定例`** — e.g. `2026_09_01_チーム内定例`. No exceptions, no decorative variants.

**Why:** the doc is referenced by date during the teirei itself ("前回定例 8/18 の…"), the esa 記事名 becomes the local filename on sync, and the report-period search (step 3) matches on that date. A title without a date breaks all three.

The frontmatter `title`, the filename, and the esa 記事名 are the same string. The H1 inside the body is the readable form (`# チーム内定例 資料（2026年9月1日）`) — that is separate and does not replace the title.

Verify after publishing: `GET /v1/teams/abist/posts/<number>` → `name` must equal the frontmatter `title`.

## Workflow

1. **Fix the dates.** Meeting date + report period (previous teirei filename → this one). State them to the user if ambiguous (e.g. today is Mon → next teirei is Tue).
2. **Copy last week's file as the template (structure only).** Read the previous `YYYY_MM_DD_チーム内定例.md` in full for section order, color legend, 俯瞰表, 直近議事録, 次アクション layout. The **案件 list itself is per-session — take it from the user's instructions**, not from last week's file.
3. **Gather report-period updates.** Filename/frontmatter dates are the reliable signal (mtime = checkout time, unreliable). Exclude the teirei folder itself so last week's file doesn't show up:
   ```
   # docs whose filename date is in the window (exclude last week's own teirei file)
   find docs -name "*.md" | grep -E "2026_?0(8_?2[4-9]|8_?3[01]|9_?01)" | grep -v "チーム内定例/2026_08_24"
   # docs whose frontmatter date/updated_at is in the window
   grep -rlE '^(date|updated_at): "2026-(08-(2[4-9]|3[01])|09-01)"' docs --include="*.md"
   ```
   ⚠ `find docs -newermt ...` over the whole tree **times out** (docs/ is ~812MB / 85k files). Use the two greps above, not mtime.
   - Read the source `.md` locally; if it isn't synced yet, sync first (see "Syncing docs from esa" below) or GET the post from the esa API.
   - ⚠ A source dated inside the window may carry a delta whose own report period straddles the teirei boundary (e.g. a 三桜定例 covering 7/3–7/9). Report only what's new since last teirei; compress the rest into `先週報告済み`.
   - For code projects, read GitHub with `gh` (already logged in via keyring; if not, export `GH_TOKEN` from `.env` GIT_TOKEN):
     - 三桜蛇腹 → `catia-jabara-design-automation`
     - FloTHERM → `catia-flotherm-prep` (+ `catia-flotherm-data-pipeline`)
   - ⚠ **Check OPEN PR branches, not just the default branch.** A week's work often lives on an unmerged PR, so `gh api repos/<repo>/commits` (default branch) returns **empty** and you wrongly read "no change." Do:
     ```
     gh pr list -R <repo> --state all --json number,title,state,updatedAt,headRefName -q '.[]|select(.updatedAt>="<開始日>")'
     gh pr view <PR番号> -R <repo> --json title,state,body,commits   # the real delta
     ```
4. **Update each 案件 section** with the standard anatomy (below). Only report this week's delta; collapse prior context into a `> **先週報告済み**：…` line.
5. **Refresh the cross-cutting sections:** top トピックサマリー, 俯瞰表 (スケジュール・課題一覧), 直近の関連議事録 (new posts only), 次アクション. Keep these consistent with the section edits.
6. **Polish the prose with `natural-japanese`** before publishing. Run it over the assembled doc (quick mode is enough for a normal week) to strip AI臭さ・翻訳調・単調なリズム. It does **not** know this team's jargon or the `**` spacing rule, so still apply the Plain-language rule and Conventions below — the two are complementary, not alternatives.
7. **Publish (only when asked).** See "Publishing to esa" below. Write `post_number` / `url` back into the frontmatter and flip `managed_by` to `esa-sync`.

## Publishing to esa

There is **no `managing-esa-posts` skill and no `publish_esa_post.py`** in this repo, and `EsaClient` (`src/abist_kb/infrastructure/sources/esa.py`) is **read-only** (`get_post` / `search_posts`). `abist-kb sync` is esa → local only. **The only write path is the esa API.**

1. Credentials: `.env` → `ABIST_KB_ESA_TEAM_NAME` (= `abist`), `ABIST_KB_ESA_ACCESS_TOKEN`. **Never print the token.**
2. Build the JSON in **Python**, not a shell heredoc (backticks and Japanese break it). Keep the script in the scratchpad; Git Bash `/tmp` is invisible to Windows Python.
3. New: `POST /v1/teams/abist/posts` (success **201**). Update: `PATCH /v1/teams/abist/posts/<number>` (success **200**).
4. Body: `{"post": {"name", "body_md", "category", "wip": false, "message"}}` — `name` = frontmatter `title`, `category` = frontmatter `category`.
5. **Verify by GET-ing the post back** and checking `number` / `name` / `category` / `wip`.

⚠ Python is **not on PATH** as `python` in Git Bash here — use `./.venv/Scripts/python.exe`. Add `PYTHONIOENCODING=utf-8` or emoji in the output raise `UnicodeEncodeError` (cp932).

## Syncing docs from esa

Use the **batch**, never `sync source`:

```
./.venv/Scripts/abist-kb.exe sync batch 設計効率化 --dry-run   # check first
./.venv/Scripts/abist-kb.exe sync batch 設計効率化
```

⚠ **`sync source <esa-id> --category ...` writes to the wrong tree.** The esa *source* `output_dir` is `docs/esa`, so it creates `docs/esa/議事録/...` — a parallel duplicate of every doc (seen as "追加 164"). The *batch* `設計効率化` has `output_dir: docs/esa/設計効率化`, which matches reality. A healthy batch dry-run looks like "追加 数件 / 更新 数件 / 変更なし 数千件".

- Locally edited files come back as **競合** and are **not** overwritten (that is the intended guard). `--force` overrides it; don't.
- **Nothing is deleted** unless you pass `--prune-orphans`. "欠落候補" is just a `source_missing` status, not a deletion.
- `docs/` is **gitignored** — git will not protect it. Back up the subtree before a real sync (`tar czf` on `docs/esa/設計効率化/議事録` ≈ 7MB; the whole `docs/` is too big and will time out).

## 案件 section anatomy

```markdown
### N. 客先 — 案件名

**担当**：氏名
**参照**：<mark>**今週の主ソース** [#NNNN](...)</mark>／過去ソース／先週報告 [#前回]

> **先週報告済み**：…（前回までの内容を1行に圧縮）— 今週は … を追記。

**今週の追加点（[#NNNN](...)）**
| 項目 | 内容 | 変化 |
|:---|:---|:---:|
| … | … | 🔵→🟢 |

#### 担当者確認（今週分）
- [ ] 未確認項目 …
- [x] 確認済み項目 …

**メモ**：（記入）
```

## Conventions

- **Color codes:** 🟢 完了/確認済 ／ 🔵 継続・進行中 ／ 🔴 未着手・遅延・要エスカレーション ／ ⚪ 保留.
- **`<mark>…</mark>`** = 先週からの重要な変更点 (this is the one thing readers scan for).
- **Links:** esa posts as `[#NNNN](https://abist.esa.io/posts/NNNN)`; GitHub issues/PRs as `[#123](https://github.com/abist-co-ltd/<repo>/issues/123)`.
- **A closing `**` needs a space after it** — `**報告期間** は`, `**完了** 🟢`, `**…** </mark>`. esa's renderer fails to close bold when a full-width character follows immediately, and the table cell breaks. To fix mechanically, pair `**` from the start of each line as alternating open/close and insert a space only after the *even* (closing) ones — a naive non-greedy regex mis-pairs and puts the space after the opener instead. Skip code fences. Verify with a checker that reports 0 violations before publishing.
- **案件 roster is NOT fixed.** The projects covered vary week to week and are **directed by the user each time (都度指示)** — do not assume last week's list carries over. Take the week's scope from the user's instructions; use last week's file only for *structure*, and pull the actual 案件 list + any additions/drops from what the user says. Add ★新規 for new ones, drop finished ones. Past examples (a reference, not a checklist): 三桜(蛇腹／評価チーム自動化／曲げ治具／検図)、TS(TMEJ／TY／TMK／SUBARU)、トヨタ自動車(設変書チェック／CATIA MCP)、名古屋受託(Claude Code)、Asquery、FloTHERM、社内(ポータル・ファイルマネージャ／交通費／AWS統合管理). If the user hasn't specified scope, ask or confirm which 案件 to include this week.

## Plain-language rule (important)

This audience is cross-team, non-specialist. **Gloss or replace internal jargon** the first time it appears; keep the term in parentheses for traceability. Past corrections on this doc:

| Jargon | Plain form |
|---|---|
| H1〜H6（仮説記号） | 「フェーズ1が使われない理由の検証」など内容で書く |
| HITL | AIが判定し要所は人が確認・修正 |
| PoC / MVP | 試験導入（PoC）／試作デモ（MVP） |
| rulepack / VDI | 提案ルール（rulepack）／顧客の検証環境（VDI） |
| REQ-…／PAT-… コード | 「入力構成を参考にした候補の同梱」等、内容で書く（コードは括弧） |

When a section gets long or technical, add a `✅ できること／❌ できないこと` table — it reads far better than prose for engineering status.

## Common Mistakes

| Symptom | Fix |
|---------|-----|
| Rebuilt the layout from scratch | Copy last week's file for *structure*; only edit deltas. |
| Assumed last week's 案件 list still applies | Roster varies; take this week's scope from the user's instructions. |
| Missed a report-period update | Search by filename AND frontmatter date, not mtime. |
| False "no change" on a code project | `commits` on the default branch is empty — check OPEN PR branches (`gh pr view <n> --json commits`). |
| Claim without a source | Every item cites an esa `#NNNN` or GitHub PR/issue. |
| Restated last week | Compress prior context into one `先週報告済み` line. |
| Jargon left raw | Gloss on first use (see table). |
| Edited section but not 俯瞰表/次アクション | Keep the cross-cutting sections in sync. |
| Published with filled post_number | Only leave it empty for genuinely new files; publish assigns it. |
| Title without the date | esa 記事名 must be `YYYY_MM_DD_チーム内定例`; it becomes the filename and the search key. |
| Looked for `managing-esa-posts` / `publish_esa_post.py` | Neither exists. `EsaClient` is read-only — publish via the esa API. |
| Ran `sync source --category` | Writes a duplicate tree under `docs/esa/議事録/...`. Use `sync batch 設計効率化`. |
| Synced without a dry-run or backup | `docs/` is gitignored. Dry-run first, back up the 議事録 subtree. |
| `**強調**` followed by a full-width char | Insert a space after the closing `**` (see Conventions). |
