# graph-plan

과제에 multi-agent graph가 필요한지 판정하고, 필요하면 GRAPH.md를 쓰는 스킬이다. 계획만 하고, 요청하지 않으면 graph를 실행하지 않는다.

## 하는 일

- **Layer test 4문항**: 사람이 모든 출력을 읽는가(→ prompt), 기계가 "done"을 검사할 수 있는가, 한 agent의 context와 domain 하나에 들어가는가(→ loop, `loop-spec`으로 넘김), 독립적으로 동시에 돌 branch가 2개 이상인가. 4개 답이 모두 graph를 가리킬 때만 graph를 쓰고, 결정한 질문을 밝힌다.
- **GRAPH.md**: node(역할, 입력, 도구, model, 예산, 출력 schema, done check), edge 계약(입력, 고정 출력 schema, timeout, 부모의 검증 방법), 공유 state(`facts.md`, `results/<node>.md`), failure route, mermaid 다이어그램, orchestrator prompt.
- **기본 한도**: 깊이 2 이하, node당 child 6개 이하(더 많으면 최대 6개씩 wave), node별 예산과 전체 예산, timeout 뒤 결과가 없는 branch는 UNKNOWN.

## 설치

저장소 루트에서:

```bash
mkdir -p ~/.claude/skills
[ -e ~/.claude/skills/graph-plan ] || cp -R graph-plan ~/.claude/skills/graph-plan
```

`references/`의 템플릿을 사용하므로 폴더 전체를 둔다. "loop" 판정을 넘겨받을 `loop-spec`도 함께 설치하는 것을 권한다.

## 사용 예

```text
graph-plan으로 "왜 일부 row에서 column X가 NULL인가?"를 조사할 graph를 계획해 줘.
PR 10개를 subagent로 나눠 리뷰하려는데, graph가 필요한지 먼저 판정해 줘.
40개 파일의 lint를 고치려는데 multi-agent로 나눌까?
```

세 번째 요청은 domain이 하나이고 기계 check가 있으므로 graph가 아니라 loop로 판정한다.

## 필요 환경

- 계획에는 추가 도구가 필요 없다.
- 만든 graph를 실행하려면 subagent나 agent team을 쓸 수 있는 호스트(Claude Code 등)가 필요하다.

## 세미나에서 확인한 것

- 세미나 실험의 PR 리뷰 과제 2개에서 graph는 single agent와 품질이 같고(6/6 vs 6/6, 14/14 vs 14/14) 비용은 2.7–5.5배였다. 리뷰가 한 context(최대 246k)에 다 들어갔기 때문이다. 경계선이면 single을 한 번 먼저 돌려 context 크기를 재 보고 정하는 것을 권한다.
- 세미나 전 수정(S1): "6 children or fewer … waves of 7 to 8"의 모순을 "최대 6개씩 wave"로 고쳤다. orchestrator prompt의 "at most 6 at a time"과 맞춘 것이다.

전체 규칙은 [SKILL.md](SKILL.md), 템플릿은 [references/](references/)에 있다.

## 설명서

[graph-plan 설명서](../manuals/graph-plan.html)
