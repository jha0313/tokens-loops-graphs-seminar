# loop-spec

agent loop를 설계하는 스킬이다. "done"을 기계가 판정할 수 있을 때만 loop를 만든다. 결과물은 `.loop/<name>/` 아래의 `LOOP.md`, verifier `check.sh`, `state.md`(hill-climb면 `measure.sh`와 `results.tsv`도)와 붙여 넣을 `/goal` 줄이다. 설계만 하고, 요청하지 않으면 loop를 시작하지 않는다.

## 하는 일

- **게이트**: "done"이 exit code, 테스트·CI 결과, 스크립트로 잰 metric 임계값, schema 검사, byte-identical 출력 중 하나일 때만 `/goal`을 쓴다. 아니면 거절하고 대안 3가지(목표를 metric으로 바꾸기, rubric을 가진 독립 리뷰어 N명, pass마다 사람 checkpoint + `/loop`)를 제안한다.
- **유형**: fix-until-green(`/goal`), hill-climb(`/goal` + `measure.sh` + `results.tsv`), watch(`/loop <interval>` 또는 예약 자동화) 중 하나를 고른다.
- **stop 계약**: `check.sh`의 마지막 줄은 `LOOP-DONE`, `LOOP-CONTINUE <reason>`, `LOOP-BLOCKED <reason>` 중 하나다. 이 token은 `check.sh`만 출력하고 에이전트는 직접 입력하지 않는다.
- **안전 기본값**: worktree나 격리된 clone에서 작업하고, merge·push·publish를 하지 않는다.

## 설치

저장소 루트에서:

```bash
mkdir -p ~/.claude/skills
[ -e ~/.claude/skills/loop-spec ] || cp -R loop-spec ~/.claude/skills/loop-spec
```

`references/`의 템플릿을 사용하므로 폴더 전체를 둔다. graph-plan이 "loop" 판정을 내리면 이 스킬로 넘기므로 함께 설치하는 것을 권한다.

## 사용 예

```text
loop-spec으로 tests/의 테스트를 모두 통과시키는 loop를 설계해 줘.
출력 byte는 그대로 두고 빌드 시간을 20% 줄이는 hill-climb loop를 만들어 줘. 예산은 10 iteration, 2시간.
README를 더 좋게 만드는 loop를 짜 줘.
```

세 번째 요청은 기계로 판정할 수 없으므로 스킬이 `/goal`을 쓰지 않고 대안을 제안한다.

설계가 끝나면 스킬이 준 `/goal` 줄을 Claude Code에 그대로 넣어 loop를 시작한다.

- goal이 설정됐다는 표시가 뜨는지 확인한다. 명령이 아니라 텍스트로 들어가면 stop 판정이 걸리지 않은 채 멈춰 있게 된다.
- headless(`claude -p`)로 돌릴 때는 항상 `--max-turns` 상한을 준다. 끝나지 않는 goal은 그 상한까지 돈다.

## 필요 환경

- `/goal` 명령이 있는 Claude Code. 스킬은 `/goal` 조건을 4,000자 안으로 쓴다.
- bash. hill-climb의 `measure.sh` 템플릿은 python3를 쓴다.

## 세미나 전 수정 사항

- **goal 템플릿(E1)**: `/goal`은 별도 evaluator 모델이 조건 충족 여부를 판정한다. 원래 템플릿은 예산을 다 써도 evaluator가 "not met"을 계속 내서 멈추지 않았다. "LOOP-BLOCKED가 보이거나 예산을 다 쓰면 이 session에서는 불가능"이라는 문장 2개를 넣은 뒤에는 강제된 추가 turn 0–2회 안에 멈췄다(3회 실행).
- **용어**: 코드 리뷰 단위는 PR, 반영은 merge로 쓴다.

전체 규칙은 [SKILL.md](SKILL.md), 템플릿은 [references/](references/)에 있다.

## 설명서

[loop-spec 설명서](../manuals/loop-spec.html)
