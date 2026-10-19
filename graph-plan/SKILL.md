---
name: graph-plan
description: 과제에 multi-agent graph가 필요한지, loop만으로 충분한지 판정한 뒤 GRAPH.md를 쓴다. GRAPH.md에는 node, edge 계약(edge contract), 공유 facts, spawn 한도, failure route, mermaid 다이어그램, orchestrator prompt가 들어간다. subagent, agent team, 병렬 조사(parallel investigation)로 fan-out하기 전에 사용한다.
---

# graph-plan

당신은 graph를 계획한다. 사용자가 요청하지 않으면 graph를 실행(launch)하지 않는다.

## Step 1: Layer test (답을 하나씩 보여 준다)
1. 무언가가 출력에 따라 행동하기 전에 사람이 모든 출력을 읽는가? 그렇다면 prompt로 충분하다. 멈춘다.
2. 기계가 "done"을 검사할 수 있는가(query, 테스트, metric, verdict 규칙)? 아니라면 아직 graph가 아니다. 사용자가 check를 먼저 정의하도록 돕는다.
3. 과제가 에이전트 하나의 context와 domain 하나에 들어가는가? 그렇다면 loop를 쓴다. `loop-spec`으로 넘긴다. 멈춘다.
4. 동시에, 서로 독립적으로 돌 수 있는 branch가 2개 이상인가? 아니라면 대신 loop에 도구를 더 준다. 멈춘다.
4개 답이 모두 graph를 가리킬 때만 graph를 쓴다. 결정을 내린 질문을 밝힌다.

## Step 2: Branch 찾기
후보를 나열한다: 독립적인 가설, 실행 경로, 서비스, 데이터 출처, 파일 묶음. 같은 context가 필요한 branch는 합친다. 다른 권한이나 checkout이 필요한 branch는 나눈다. level마다 branch는 최대 6개다.

## Step 3: Node
node마다 쓴다: 이름, 역할(한 줄), 입력, 도구와 skill, model과 effort, 예산(token 또는 분), 출력 schema, done check.
기본값:
- orchestrator는 하나. 계획·라우팅·검증·병합을 한다. 무거운 작업을 직접(inline) 하지 않는다. 그러면 다른 모든 branch가 막히기 때문이다.
- worker는 짧게 산다: spawn, 반환, 종료. 진짜 격리가 필요할 때만 node를 오래 유지(persistent)한다: 별도 checkout, 별도 권한, 독립 리뷰.
- 모으고 검색하는 데는 싼 model을 쓴다. 추론하고 검증하는 데는 강한 model을 쓴다.
- "root cause를 찾았다"나 "done" 주장마다 새 context의 verifier node를 둔다. 그 주장을 반박(refute)해 보라고 지시한다.

## Step 4: Edge 계약 (graph가 가장 많이 실패하는 곳)
모든 edge에 쓴다:
- In: facts 파일 경로와 정확한 질문.
- Out: 고정 schema(verdict, 증거 링크, confidence, 열린 질문).
- Timeout.
- 부모가 결과를 검증하는 방법.
context는 계약에 실렸을 때만 edge를 건넌다. fire-and-forget이 아니라 fire-and-verify를 쓴다.

## Step 5: 공유 state
- `facts.md`: append-only, 검증된 사실, 한 줄에 출처 하나. orchestrator가 쓴다. 모든 worker가 이 파일을 먼저 읽으므로, 같은 사실을 다시 찾는 worker가 없다.
- `results/<node>.md`: 파일마다 writer 하나.
- 두 node가 같은 파일을 편집하지 않는다. 코드 변경을 merge해야 하면 끝에 committer node 하나를 둔다.

## Step 6: 한도 (기본값. 이유가 있을 때만 바꾼다)
- 깊이 2 이하: orchestrator, 그다음 worker. worker는 spawn할 수 없다.
- node당 child 6개 이하. 더 큰 fan-out은 최대 6개씩 wave로 나눠 돌린다.
- node별 예산과 전체 예산. 추정치를 보여 준다: node 예산의 합.
- Orphan 규칙: timeout이 지나도 결과가 없는 branch는 UNKNOWN이다. 끝없이 재시도하지 않는다.

## Step 7: Failure route
| 이벤트 | Route |
|---|---|
| worker 과제 실패 | 새 worker와 실패 메모로 한 번 재시도한다. 그다음은 UNKNOWN. |
| 시스템·환경 오류 (permission, ACL, infra, quota) | graph를 멈추고 보고한다. |
| 충돌하는 verdict | verifier가 증거로 결정하거나, 사람에게 escalate한다. |
| 모든 branch가 UNKNOWN 또는 refuted | 새 planner로 한 번 다시 계획한다. 그다음 멈춘다. |
| 같은 위임을 두 번 보냄 (예: compaction 뒤) | branch id로 중복을 제거한다. |

## Step 8: GRAPH.md 쓰기
`references/graph-template.md`를 채운다. mermaid 다이어그램과 `references/orchestrator-prompt.md`의 orchestrator prompt를 넣는다. Claude Code에서는 worker를 subagent나 agent team으로 돌린다.

## 사용자에게 줄 출력
판정(prompt, loop, graph)과 그것을 결정한 질문을 준다. graph면 GRAPH.md 경로, 다이어그램, 비용 추정, launch prompt를 준다.

## 예시
- "왜 일부 row에서 column X가 NULL인가?" Graph: 실행 경로마다 branch 하나(예: cached vs uncached, path A vs path B), 그리고 source/producer branch와 schema/sink branch. facts pack에는 column 정의, producer, pipeline config가 들어간다. verifier가 query로 원인을 확인한다.
- "생성된 변경에 비교 테스트 5개를 돌리고 모든 실패를 설명해." Graph: 병렬 테스트 branch 5개, 그다음 classifier(expected, known noise, unexpected), 그다음 unexpected 실패마다 investigator 하나, 그다음 test plan으로 merge.
- "40개 파일의 lint를 고쳐." graph 아님: domain 하나, 기계 check, 조율 불필요. loop를 쓰거나, edge 없는 단순 병렬 batch를 쓴다.
