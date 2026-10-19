---
name: token-efficiency-summary
description: "Claude Code session의 token 사용량(token usage)을 분석해 낭비(waste)를 찾고, cost를 줄이는 실행 가능한 권고를 준다."
argument-hint: <session-id or flags>
allowed-tools: Read, Bash, AskUserQuestion
---

# Token 효율 요약 (Token efficiency summary)

이 스킬은 읽기 전용이다. Python 스크립트가 Claude Code transcript를 읽고 JSON을 출력한다. 당신은 그 JSON으로 보고서를 쓴다. transcript는 절대 수정·이동·삭제하지 않는다.

인자: `$ARGUMENTS`

## 1. 스크립트 설치 (첫 사용)

호출할 때마다 한 번 실행한다. 설치된 사본이 없거나 내용이 다를 때만 스크립트를 복사한다.

```bash
mkdir -p ~/.claude/scripts
cmp -s "${CLAUDE_SKILL_DIR}/scripts/analyze-tokens.py" ~/.claude/scripts/analyze-tokens.py \
  || cp "${CLAUDE_SKILL_DIR}/scripts/analyze-tokens.py" ~/.claude/scripts/analyze-tokens.py
chmod +x ~/.claude/scripts/analyze-tokens.py
```

## 2. 인자를 플래그로 바꾸기

| 인자 | 스크립트 플래그 |
|---|---|
| (없음) | `--current` |
| `<session-id>` (UUID, 고유한 UUID prefix, 또는 `agent-<id>`) | `--session <id>` |
| `--full` | `--full` (INFO 탐지기 2개 추가) |
| `--last 7d` (`7d`, `2w`, `1m`) | `--last 7d` |
| `--last 30d --aggregate` | `--last 30d --aggregate` |
| `--project <name>` | `--project <name>` |
| `--turn <n>` | `--turn <n>` (session 하나가 필요) |
| `--compare <A> <B>` | `--compare <A> <B>` |
| `--detail` | `--detail` (turn별 표, `--full`과 무관) |
| `--dashboard <path>` | `--dashboard <path>` (한국어 HTML 대시보드도 쓴다. `--aggregate`나 session 하나가 필요) |
| `--dashboard-ids` | `--dashboard-ids` (비용 상위 transcript id를 대시보드에 넣는다. 기본은 끔) |

- 플래그는 받은 그대로 넘긴다. 함께 쓸 수 있다(예: `<id> --turn 5 --full`).
- session id, `--last`, `--aggregate`, `--project`, `--compare`가 모두 없으면 `--current`를 붙인다.
- `--last` 없이 `--aggregate`나 `--project`를 쓰면 최근 30일을 쓴다.
- 인자가 플래그도 session id도 아니면 AskUserQuestion으로 묻는다. 물을 수 없으면 무엇을 이해하지 못했는지 말하고 멈춘다.
- 항상 `--format json`을 붙인다.
- 사용자가 대시보드, HTML 페이지, 공유할 페이지를 원하면 `--dashboard <path>`를 붙인다. 사용자가 경로를 정하지 않으면 현재 디렉터리 아래 경로를 쓴다. `--dashboard-ids`는 사용자가 페이지에 session id를 넣어 달라고 할 때만 붙인다. 그러면 JSON 출력에 절대 경로를 담은 `dashboard` 키가 생긴다. 그 경로를 사용자에게 알려 준다. 파일을 열지는 않는다.

실행:

```bash
CLAUDE_SESSION_ID="${CLAUDE_SESSION_ID}" python3 ~/.claude/scripts/analyze-tokens.py <flags> --format json
```

출력에 `error` 키가 있으면 오류를 보여 주고 멈춘다. 출력이 클 수 있다. 보고서를 쓰기 전에 끝까지 읽는다.

## 3. 어떤 transcript를 세는가

- 메인 session: `~/.claude/projects/<project-dir>/<session-id>.jsonl`.
- Subagent: `~/.claude/projects/<project-dir>/<session-id>/subagents/agent-<id>.jsonl`. subagent 파일 하나하나가 독립된 transcript다. 메인 session의 숫자에는 그 subagent가 들어가지 않는다.
- `--last`와 `--aggregate`는 파일 수정 시각이 기간 안에 있는 모든 메인·subagent transcript를 가져온다. 둘은 따로 센다: "session N개 + subagent M개"(N sessions + M subagents).
- `--project`는 프로젝트 디렉터리 이름과 맞춘다. `video_editor`는 `-Users-me-Projects-video-editor`와 맞고, `...-video-editor-pm`과는 맞지 않는다.
- turn은 user record마다 시작하고, tool result도 user record다. 그래서 turn 번호는 내 프롬프트 수가 아니라 tool 왕복 횟수를 센다.

## 4. 보고서 쓰기

천 단위 구분 기호를 쓴다(`1,234,567`). 숫자는 표로 보여 준다. 비율은 백분율로 쓴다.

**단일 session** (`mode: "session"`):
1. 머리말: session id, project, 소요 시간(`session.duration_seconds`를 `Xh Ym` 형식으로), model, turn 수. subagent면 종류(kind)도 적는다.
2. Token 표: input, output, cache read(`cache_hit_ratio` 포함), cache write(5분과 1시간), 최대 context, 추정 비용. 표 아래에 비용 계산 방식을 한 줄로 쓴다: "비용: 모델별 호출당 정가, cache write는 TTL(5분 또는 1시간)별 단가로 계산."(Cost: list price per call, by model; cache writes priced by their TTL (5-minute or 1-hour).)
3. Cache 경제(`cache_economics`): `no_cache_cost_usd`와 `no_cache_ratio`로 "cache가 없었다면 $X(실제의 N배)" 한 줄, `breaks.by_cause`로 break 표(원인, 횟수, `lost_usd`), `cache_write_5m_tokens`가 0보다 크면 `ttl_1h_simulation`으로 1시간 TTL 한 줄(`delta_usd`, `breaks_avoided`).
4. 이슈: `tokens_wasted` 순(JSON 순서)으로 정렬한다. 최대 15행. `total_issues`가 15보다 크면 "N개 중 15개 표시"(15 of N shown)라고 쓴다. 열: 굵은 severity(`**HIGH**`), signal, 무슨 일이 있었는지(`description`), turn, token, 배분한 비용(`cost_wasted_usd`).
5. 권고: `estimated_tokens_saved` 순(JSON 순서)으로 정렬한다. 열: signal, 권고, token, `estimated_savings_pct`(input 쪽 token 대비 %), 배분한 비용(`estimated_savings_usd`).
- `--turn`이 있으면 `turn_detail` 절을 추가한다: 그 turn을 시작한 프롬프트나 tool result, 각 model call(context, cache hit, output), 호출한 도구, 그 turn의 이슈.
- `--detail`이 있으면 `turns_detail`로 turn별 표를 추가한다(turn, calls, input, output, cache read, cache write, context, tools). 40행을 넘으면 context가 가장 큰 40개 turn만 보여 주고 그렇다고 밝힌다.

**기간** (`mode: "list"`): 기간, project, "session N개 + subagent M개"를 담은 머리말, 전체 token 표, 그다음 비용순 session 표(id, kind, turns, cost, cache hit, max context, issues, top signal). 최대 20행.

**집계** (`mode: "aggregate"`):
1. 머리말: 기간, project, "session N개 + subagent M개", model call 수.
2. 합계 token 표와 비용 계산 방식 한 줄, 비용 구성(`cost_split_usd`: cache read, cache write, output, input).
3. Cache 경제(`cache_economics`): "cache가 없었다면" 한 줄, 메인 session 대 subagent(`by_kind`: 비용, 5분·1시간 write, break), 원인별 break(`by_cause`), subagent 1시간 TTL 시뮬레이션(`by_kind.subagent.ttl_1h_simulation`).
4. Signal 표: 굵은 severity, signal, `frequency`(예: "8/12 sessions, 1/3 subagents"), issues, tokens, 배분한 비용(`cost_wasted_usd`). 배분한 비용 순으로 정렬한다. `large_tool_result`와 `oversized_read`는 `issues` 중 `image_or_pdf_issues`도 함께 적는다.
5. 고칠 것(`actions`, 이미 ROI 순): 고칠 것, 30일당 금액(`usd_30d`), `basis`, 노력(분), 위험, `per_run`. 그다음 상위 3개(`top_actions`)와 각각의 `how`.
6. 비용 상위 transcript.

**비교** (`mode: "compare"`): A, B, 차이(delta) 열이 있는 표 하나: turns, model calls, input, output, cache read, cache write, cache hit, max context, estimated cost, tool calls, issues. 그다음 A에만, B에만, 둘 다에 있는 signal. 그다음 각 session의 상위 이슈 5개.

## 5. 알려진 한계 (해당할 때 말한다)

- `cost_wasted_usd`는 배분한 상한이지 입증된 절감이 아니다. 분석기는 호출마다 실제로 낸 돈(input, cache write, cache read, output)에서 한 번만 떼어 한 패턴에만 준다. cache break를 먼저, 그다음 고정된 탐지기 순서로 뗀다. 그래서 금액이 겹치지 않고 합(`total_cost_wasted_usd`)이 `estimated_cost_usd`를 넘지 않는다. "최대 $X"라고 말하고 "$X를 아낀다"고 말하지 않는다.
- `session_too_long`은 금액을 떼지 않는다. 그 비용은 `context_growth_no_compaction`과 cache break에 들어간다. `model_mismatch`는 Sonnet 5.5와의 output 단가 차이만 뗀다. 이미지와 PDF 결과는 장당 1,600 token으로 계산한다.
- TTL 시뮬레이션은 5분 write를 1시간 write 값으로 다시 계산하고, 간격이 5–60분인 break를 cache read로 바꾼다. 추정치다. TTL이 달랐다면 바뀌었을 호출 자체는 모델링하지 않는다.
- 고칠 것의 30일당 금액은 기간을 선형으로 늘린 값이다. 노력(분)은 처음 설정에 드는 대략의 추정이다.
- `tokens_wasted`는 그 패턴이 건드린 token의 크기(상한)다. token 범주는 겹치므로 더하지 않는다.
- 횟수는 비용이 아니다. 많은 session에서 뜨는 signal도 token은 적을 수 있다(실사용 테스트에서 `large_tool_result`는 352개 session 중 298개에서 떴지만 비용의 약 3%만 설명했다). 횟수가 아니라 배분한 비용이나 token으로 순위를 매긴다.
- 비용 계산기는 서로 다를 수 있다. 21회 점검에서 이 분석기는 CLI `total_cost_usd`와 센트 단위까지 맞았다. 재시도된 요청이 transcript에 없는 run만 달랐다. 어떤 방식을 썼는지 늘 밝힌다.
- `large_tool_result`와 `oversized_read`는 raw result의 글자 수를 센다. 이미지와 PDF 결과는 base64 텍스트라서 실제 token 비용보다 훨씬 커 보인다. 이런 이슈의 description에는 "image/PDF base64"가 들어 있다. 이들에는 offset/limit을 권하지 않는다.
- `exploration_overhead`는 Read/Grep/Glob 호출과 읽기 전용 shell 명령(`grep`, `rg`, `find`, `ls`, `cat`, `sed -n`, `git log`/`show`/`diff` 등. `&&`로 이은 명령은 하나씩 센다)을 센다. 다른 read 탐지기(`near_duplicate_read`, `oversized_read`, `read_after_edit`)는 여전히 Read 도구만 본다.
- `system_prompt_bloat`는 Claude Code가 attachment로 기록한 선택 prefix만 센다: skill 목록, MCP instructions, deferred tool 이름, agent 목록. 내장 prompt, 내장 도구, CLAUDE.md는 세지 않는다. attachment 기록이 없는 transcript는 system token에서 내장 하한 22,000 token을 뺀 값을 쓴다.
