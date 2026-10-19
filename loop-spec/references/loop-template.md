# LOOP: <name>

목표(Goal): <측정할 수 있는 한 문장>
유형(Type): fix-until-green | hill-climb | watch

| # | 질문 (Question) | 답 (Answer) |
|---|---|---|
| 1 | 트리거 (Trigger) | 지금 `/goal` / `/loop <interval>` / 예약 자동화(scheduled automation) / 이벤트(event) |
| 2 | 작업 출처 (Work source) | <다음 항목을 가져오는 곳: 실패 테스트 목록, state.md의 아이디어 목록, 큐> |
| 3 | Context와 도구 (Context and tools) | <skill, 파일, 명령; model과 effort> |
| 4 | Workspace와 권한 경계 (Workspace and permission boundary) | <worktree 또는 clone 경로; 허용 명령; 금지 행동> |
| 5 | Verifier | `.loop/<name>/check.sh` (+ `measure.sh`). 마지막 줄: LOOP-DONE / LOOP-CONTINUE / LOOP-BLOCKED |
| 6 | State | `.loop/<name>/state.md`, `.loop/<name>/results.tsv` |
| 7 | 예산 (Budget) | <최대 iteration 수>, <최대 시간>, <진전 없는 iteration 최대 수> |
| 8 | Escalation | LOOP-BLOCKED; 예산 소진; 같은 실패 3번; 경계 밖 행동 |
| 9 | 멈춤 (Stop) | <정확한 DONE 조건> |

## 정확성 guard (Correctness guard, hill-climb 전용)
`<command>`: 채택하는 모든 변경이 통과해야 한다.

## Noise (hill-climb 전용)
Baseline: <N>번 run에서 median <x>, spread <y>. median을 <threshold>보다 크게 개선하는 변경만 받아들인다.

## 금지 (Never)
- merge, push, publish.
- `<path>` 밖 편집.
- loop가 시작하지 않은 job이나 process 중지.

## 시작 (Start)
```
<the /goal line>
```
