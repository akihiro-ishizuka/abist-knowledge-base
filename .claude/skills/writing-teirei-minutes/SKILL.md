---
name: writing-teirei-minutes
description: Use when asked to assemble/まとめる the weekly チーム内定例 資料 (team internal weekly meeting minutes) in this docs repo — e.g. "7/7〜の進捗をチーム内定例でまとめて", update the 議事録 for this week's teirei, or produce the case-by-case progress doc. Covers gathering report-period updates from esa posts and GitHub, and the standard section layout.
---

# Writing チーム内定例 Minutes

## Overview

The weekly **チーム内定例** doc is a **diff-based, case-by-case progress report**: for each 恒常案件 (standing project), it records only what changed since last week's teirei, cites the source esa 記事 / GitHub, and lists per-owner follow-ups. **Reuse last week's file as the structural template** and update the deltas — do not invent a new layout.

**Core principle:** report the *delta* (先週報告済みは省略), cite every claim to an esa `#NNNN` or GitHub PR/issue, and keep language plain enough for non-specialist teammates.

**REQUIRED SUB-SKILL:** Use `managing-esa-posts` to read source posts (`fetch`) and to publish/patch the finished doc.

## Where the file lives

```
docs/チーム内定例/議事録/設計効率化/チーム内定例/YYYY_MM_DD_チーム内定例.md
```
- Teirei is usually **Tuesday**. New file's date = the meeting date. Report period = **前回teirei日 〜 今回teirei日**.
- Ignore archive duplicates like `docs/2026年MM月/...` — the 正本 is under `docs/チーム内定例/...`.
- New unpublished file: leave frontmatter `post_number:` / `url:` **empty** (esa assigns on publish). Set `date`/`updated_at` to the meeting date.

## Workflow

1. **Fix the dates.** Meeting date + report period (previous teirei filename → this one). State them to the user if ambiguous (e.g. today is Mon → next teirei is Tue).
2. **Copy last week's file as the template (structure only).** Read the previous `YYYY_MM_DD_チーム内定例.md` in full for section order, color legend, 俯瞰表, 直近議事録, 次アクション layout. The **案件 list itself is per-session — take it from the user's instructions**, not from last week's file.
3. **Gather report-period updates.** Filename/frontmatter dates are the reliable signal (mtime = checkout time, unreliable). Exclude the teirei folder itself so last week's file doesn't show up:
   ```
   # docs whose filename date is in the window (exclude the teirei dir)
   find docs -name "*.md" | grep -E "2026_?07_?(08|09|10|11|12|13|14)" | grep -v "チーム内定例/議事録.*/チーム内定例/"
   # docs whose frontmatter date/updated_at is in the window
   grep -rlE '^(date|updated_at): "2026-07-(0[89]|1[0-4])"' docs --include="*.md"
   ```
   - Read the source `.md` locally, or `fetch` from esa if not synced (see managing-esa-posts).
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
6. **Publish (if asked)** via managing-esa-posts (`publish_esa_post.py`), which fills `post_number`/`url` back in.

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
