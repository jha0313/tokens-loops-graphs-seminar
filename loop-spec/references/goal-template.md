# /goal 줄 템플릿

아래를 복사한 뒤 모든 <placeholder>를 바꾼다.

```text
/goal <one-sentence goal>. Work only in <workspace>. At the start of every turn, read .loop/<name>/LOOP.md and .loop/<name>/state.md.
MET only when your most recent turn contains the complete stdout of `bash .loop/<name>/check.sh` in a fenced block and its last line is exactly LOOP-DONE. Only check.sh may print LOOP-DONE, LOOP-CONTINUE or LOOP-BLOCKED. Never type them yourself.
If the last line is LOOP-BLOCKED, stop and report the reason. Do not work around it. Once LOOP-BLOCKED is shown, the goal is impossible in this session.
These are NOT reasons to stop: failing tests, lint errors, a rejected change, a slow or noisy measurement, an idea that did not work, many iterations without a win (until the budget in LOOP.md is used).
Each turn, do exactly one iteration: take the next item from state.md, make one change, run check.sh, keep or revert the change, and update state.md. End every turn with no background shell and no subagent running.
Never merge, push, publish, or edit outside <workspace>. Stop after <N> iterations or <H> hours and report the best result. Once the budget is used, the goal is impossible in this session.
```
