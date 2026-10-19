# token-efficiency-summary

Claude Code session이 token을 어디에 썼는지 보여 주는 읽기 전용 스킬이다. Python 분석기 `scripts/analyze-tokens.py`가 transcript를 읽고 model call마다 token을 센 뒤, 고정 규칙 waste 탐지기(기본 21개, `--full`이면 23개)를 돌려 JSON을 출력한다. 스킬은 그 JSON을 표 중심 보고서로 정리한다.

## 하는 일

- 현재 session, 특정 session, 기간(`--last 7d`), 기간 집계(`--aggregate`), 두 session 비교(`--compare`)를 보고한다.
- input, output, cache read·write(5분·1시간), cache hit 비율, 최대 context, 추정 비용을 표로 보여 준다. 이슈는 `tokens_wasted` 순, 권고는 `estimated_tokens_saved` 순이다.
- cache 경제성을 계산한다: cache가 없었다면 든 비용, session 중간의 cache 깨짐(원인별: 5분 TTL 만료, 1시간 넘는 공백, compaction, prompt 변경, model 전환), 1시간 TTL로 바꿨을 때의 추정 차액.
- 패턴별 비용은 실제 call 비용에서 한 번만 떼어 준다. 그래서 겹치지 않고, 합이 실제 비용을 넘지 않는다. 집계 모드는 고칠 거리마다 30일 기준 달러, 노력, 위험을 붙여 ROI 순으로 정렬한다.
- 메인 session과 subagent transcript를 따로 센다.
- `--dashboard <path>`를 주면 한국어 HTML 대시보드 한 장을 쓴다. 외부 로드 없이 inline SVG·CSS만 쓰며, 기본값으로는 session ID, 파일 경로, prompt 내용을 넣지 않는다(`--dashboard-ids`를 주면 비싼 transcript ID를 넣는다).
- transcript를 수정·이동·삭제하지 않는다. 분석기가 쓰는 파일은 `--save-history`의 history 파일과 `--dashboard`의 HTML 파일뿐이다.

## 설치

`SKILL.md`가 `scripts/`를 사용하므로 폴더 전체를 둔다. 저장소 루트에서:

```bash
mkdir -p ~/.claude/skills
[ -e ~/.claude/skills/token-efficiency-summary ] || cp -R token-efficiency-summary ~/.claude/skills/token-efficiency-summary
```

첫 실행 때 스킬이 분석기를 `~/.claude/scripts/analyze-tokens.py`로 복사한다. 설치된 사본이 없거나 내용이 다를 때만 복사한다.

## 사용 예

```text
/token-efficiency-summary
/token-efficiency-summary --last 7d
/token-efficiency-summary --last 30d --aggregate
/token-efficiency-summary --last 30d --aggregate --project <name> --dashboard token-dashboard.html
/token-efficiency-summary <session-id> --turn 5 --full
/token-efficiency-summary --compare <A> <B>
```

자연어로도 부를 수 있다.

```text
이번 session에서 token이 어디에 쓰였는지, cost를 줄일 방법이 있는지 분석해 줘.
최근 30일 Claude Code 사용에서 가장 비싼 패턴을 찾아 줘.
```

분석기만 직접 돌릴 수도 있다(출력은 JSON).

```bash
python3 token-efficiency-summary/scripts/analyze-tokens.py --last 7d --format json
python3 token-efficiency-summary/scripts/analyze-tokens.py --help
```

## 필요 환경

- Python 3.8+. 표준 라이브러리만 쓰며 네트워크를 쓰지 않는다.
- 로컬 Claude Code transcript(`~/.claude/projects/`).
- 테스트(33개): `python3 -B -m unittest discover -s token-efficiency-summary/scripts/tests`

## 한계

- 비용은 모델별 정가로 계산한 추정치다. cache write는 TTL(5분·1시간)별 가격으로 계산한다.
- 패턴별 달러는 귀속 상한이다. "최대 $X"로 말하고 절감 확정액으로 인용하지 않는다. TTL 시뮬레이션과 30일 환산도 추정이다.
- `exploration_overhead`는 Read/Grep/Glob과 읽기 전용 shell 명령(`grep`, `rg`, `find`, `cat`, `sed -n`, `git log` 등)을 함께 센다. 다른 read 탐지기는 아직 Read 도구만 본다.
- `system_prompt_bloat`는 attachment로 기록된 선택 prefix(skill 목록, MCP 안내, deferred tool 이름, agent 목록)만 센다. 기본 prompt, 기본 tool, CLAUDE.md는 세지 않는다.

전체 규칙은 [SKILL.md](SKILL.md)에 있다.

## 설명서

[token-efficiency-summary 설명서](../manuals/token-efficiency-summary.html)
