---
name: loop-spec
description: 기계가 판정하는 stop 조건을 갖춘 agent loop를 설계한다. LOOP.md, check.sh, 바로 쓸 수 있는 /goal 줄을 쓰고, "done"을 기계로 검사할 수 없으면 거절한다. /goal 또는 /loop 설정, fix-until-green 작업, run time이나 build time 같은 metric 최적화에 사용한다.
---

# loop-spec

당신은 loop를 설계한다. 사용자가 요청하지 않으면 loop를 시작하지 않는다.

## Step 1: 사실 수집 (질문은 최소로)
다음은 저장소, 문서, 대화에서 직접 찾는다: 테스트·빌드·metric 명령, state를 둘 위치, 권한 경계(permission boundary).
찾을 수 없는 것만 사용자에게 묻는다: 예산(iteration 수, 시간), metric 목표, loop가 절대 하면 안 되는 행동. 질문은 한 차례(one round)까지만 한다.

## Step 2: 게이트: 기계가 "done"이라고 말할 수 있는가?
"done"이 다음 중 하나일 때만 받아들인다: exit code, 테스트 또는 CI 결과, 스크립트로 잰 metric 임계값, schema 검사, byte-identical 출력.
아니면 /goal을 쓰지 **않는다**. 이렇게 말한다: "기계로 검사할 수 있는 done이 없습니다. 이 loop는 예산이 끝날 때만 멈춥니다."(No machine-checkable done. A loop here stops only on budget.) 그다음 아래를 제안한다:
1. 목표를 metric으로 바꾼다(무언가를 세거나 점수를 매기는 스크립트).
2. 작성된 rubric을 가진 독립 리뷰어 N명을 check로 쓴다. 스크립트보다 약한 check라고 표시한다.
3. pass마다 사람이 확인하는 checkpoint를 두고, `/goal` 대신 `/loop`를 쓴다.

## Step 3: loop 유형 고르기
| 유형 | 쓸 때 | 엔진 |
|---|---|---|
| fix-until-green | 테스트, lint, CI가 통과해야 할 때 | `/goal` |
| hill-climb | 정확성 guard를 지키면서 noise가 있는 metric을 개선할 때 | `/goal` + `measure.sh` + `results.tsv` |
| watch | 끝 상태가 없는 반복 확인 | `/loop <interval>` 또는 예약 자동화(scheduled automation). 일회성 wakeup을 쓴다. sleep으로 polling하지 않는다. |

## Step 4: 9개 질문에 답하기
`references/loop-template.md`를 채워 `.loop/<name>/LOOP.md`에 쓴다. 모든 답은 구체적이어야 한다: 명령, 경로, 숫자 중 하나.

## Step 5: verifier `check.sh` 쓰기
`references/check-sh-template.md`에서 시작한다. 계약:
- 마지막 줄은 정확히 `LOOP-DONE`, `LOOP-CONTINUE <reason>`, `LOOP-BLOCKED <reason>` 중 하나다.
- `LOOP-BLOCKED` = 시스템·환경 오류: permission denied, 자격 증명 없음(missing credentials), ACL, quota, 인프라 장애(infra failure), incident, 도구 없음, 디스크 가득 참. loop는 멈추고 도움을 요청한다.
- `LOOP-CONTINUE` = 과제 오류: 실패하는 테스트, lint, 회귀(regression), 거절된 변경. loop는 계속한다.
- 이 token은 `check.sh`만 출력한다. 에이전트는 절대 직접 입력하지 않는다.
- check는 결정적(deterministic)이고 멱등(idempotent)이며 빠르다(iteration 한 번의 10% 미만). `.loop/<name>/` 아래에만 쓴다.

hill-climb면 `references/measure-sh-template.md`로 `measure.sh`도 쓴다:
- baseline을 먼저 N번 잰다(기본 3). median과 spread를 `results.tsv`에 기록한다.
- median이 max(2 × spread, 사용자 임계값)보다 크게 개선되고, 그리고(AND) 정확성 guard(테스트, byte-identical 출력, 같은 verdict)를 통과할 때만 변경을 받아들인다.
- 있으면 wall time보다 부하와 무관한 metric(instruction count, 내부 timer)을 쓴다.
- 입력을 고정한다(고정 commit, live config나 data pull 없음). 그래야 run끼리 비교할 수 있다.
- 거절한 변경은 되돌리고 이유를 기록한다. 검증하지 않은 변경을 쌓지 않는다.

## Step 6: State
`.loop/<name>/state.md`에는 목표, 현재 최고 결과, 시도한 아이디어(유지·거절 여부와 이유), 다음 아이디어, 마지막 check 출력을 둔다. iteration마다 이 파일을 가장 먼저 읽고 가장 나중에 갱신한다. job ID와 PR 번호도 여기에 둔다. 대화나 `/tmp`에만 두지 않는다.

## Step 7: 예산과 escalation
정한다: 최대 iteration 수, 최대 시간, 진전 없는 연속 iteration의 최대 수(기본 3: 한 번 다시 계획하고, 그다음 멈춘다).
다음 경우 escalate한다: `LOOP-BLOCKED`, 예산 소진, 같은 실패 3번, 권한 경계 밖의 행동.

## Step 8: /goal 줄 내보내기
`references/goal-template.md`를 채운다(최대 4,000자). 반드시 넣는다:
- MET은 fenced block에 담긴 `check.sh` stdout의 마지막 줄이 `LOOP-DONE`일 때만.
- `LOOP-BLOCKED`는 멈추고 보고하라는 뜻이다.
- 멈출 이유가 아닌 것(NOT reasons to stop)의 목록.
- turn 하나에 범위가 정해진 iteration 하나. turn이 끝날 때 background shell이나 subagent가 돌고 있지 않을 것.
- merge, push, publish, workspace 밖 편집은 절대 하지 않는다.

## 안전 기본값 (LOOP.md에 항상 넣는다)
- 사용자의 main checkout이 아니라 worktree나 격리된 clone에서 작업한다.
- merge, push, publish를 하지 않는다. loop가 시작하지 않은 job과 process를 멈추지 않는다.
- 파괴적이거나 공유 상태를 바꾸는 행동에는 사람의 gate가 필요하다.

## 사용자에게 줄 출력
파일 경로, code block에 담은 /goal 줄, 시작 명령, 3줄로 쓴 stop 규칙을 준다. 요청이 없으면 loop를 시작하지 않는다.

## 예시
- "tests/의 테스트를 모두 통과시켜": fix-until-green. `check.sh`가 테스트를 돌린다. 통과하면 DONE.
- "출력은 바꾸지 말고 codegen wall time을 줄여": hill-climb. `measure.sh`가 regen 시간을 3번 잰다. guard는 생성된 파일과 baseline snapshot의 `diff -r`이다(생성 파일 서명이나 timestamp가 든 줄은 무시한다).
- "README를 더 좋게 만들어": 거절(기계 check 없음). 리뷰어 3명의 rubric이나 사람 checkpoint를 제안한다.
