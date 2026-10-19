# Long Running Agent의 시대, 사람의 역할은?

**Token · Loop · Graph 엔지니어링, 그리고 사람** — 삼성전자 AI센터 3차 세미나 자료

<div align="center">

**Jae Ha** — Meta 시니어 엔지니어 · 9년 차 · Dev Productivity champion  
<sub>메타에서 실제로 해보고 배운 것을 계속 공유합니다</sub>

[![YouTube @sv.developer](https://img.shields.io/badge/YouTube-%40sv.developer-FF0000?style=for-the-badge&logo=youtube&logoColor=white)](https://www.youtube.com/@sv.developer)
[![Course](https://img.shields.io/badge/Course-Claude%20Code%20%26%20Agentic%20Workflow-1F6FEB?style=for-the-badge&logo=bookstack&logoColor=white)](https://fastcampus.co.kr/data_online_svvibecoding)
[![LinkedIn](<https://img.shields.io/badge/LinkedIn-jae--sang--ha-0A66C2?style=for-the-badge&logo=data:image/svg%2Bxml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHZpZXdCb3g9JzAgMCAyNCAyNCc+PHRleHQgeD0nMS41JyB5PScxOS41JyBmb250LWZhbWlseT0nQXJpYWwsSGVsdmV0aWNhLHNhbnMtc2VyaWYnIGZvbnQtd2VpZ2h0PSdib2xkJyBmb250LXNpemU9JzE5JyBmaWxsPSd3aGl0ZSc+aW48L3RleHQ+PC9zdmc+>)](https://www.linkedin.com/in/jae-sang-ha/)
[![KakaoTalk Open Chat](https://img.shields.io/badge/Open%20Chat-KakaoTalk-FEE500?style=for-the-badge&logo=kakaotalk&logoColor=000000)](https://open.kakao.com/o/g3Os2Lri)

<table>
<tr><td align="right">▶️ <b>유튜브</b></td><td><a href="https://www.youtube.com/@sv.developer">@sv.developer</a> — Claude Code · Agentic Engineering</td></tr>
<tr><td align="right">🎓 <b>강의</b></td><td><a href="https://fastcampus.co.kr/data_online_svvibecoding">실리콘밸리 엔지니어의 Claude Code — 바이브 코딩 &amp; 에이전틱 워크플로우 실전 로드맵</a> (일부 강의 무료 공개)</td></tr>
<tr><td align="right">💼 <b>링크드인</b></td><td><a href="https://www.linkedin.com/in/jae-sang-ha/">jae-sang-ha</a></td></tr>
<tr><td align="right">💬 <b>오픈카톡방</b></td><td><a href="https://open.kakao.com/o/g3Os2Lri">open.kakao.com/o/g3Os2Lri</a> — 세미나 질문</td></tr>
</table>

</div>

AI agent 작업을 token(호출 한 번의 비용) → loop(기계가 판정하는 멈춤) → graph(여러 agent의 분기와 계약) 세 층으로 나눠 실험 결과와 함께 살펴보고, AI가 나아질수록 사람이 무엇을 더 잘해야 하는지로 마무리한 세미나의 **스킬 설명서**와 발표에서 사용한 **스킬 3종**(token-efficiency-summary · loop-spec · graph-plan)을 담은 저장소다. 발표 deck(발표 노트를 뺀 화면판)은 `slides/index.html`에 있다.

## 빠른 시작

### 설명서·슬라이드 열기

GitHub에서 HTML 파일을 클릭하면 소스가 보인다. clone한 뒤 저장소 루트에서 정적 서버를 띄우고 브라우저로 연다.

```bash
git clone https://github.com/jha0313/tokens-loops-graphs-seminar.git
cd tokens-loops-graphs-seminar
python3 -m http.server 8000
# 브라우저: http://localhost:8000/manuals/token-efficiency-summary.html
#           http://localhost:8000/manuals/loop-spec.html
#           http://localhost:8000/manuals/graph-plan.html
#           http://localhost:8000/slides/index.html   (발표 deck)
```

deck은 ← → 키로 넘기고, S를 누르면 슬라이드 목록이 나온다.

### 스킬 설치

Claude Code 기준. 저장소 루트에서 실행하면 세 폴더를 `~/.claude/skills/`에 통째로 복사하고, 이미 있는 대상은 덮어쓰지 않고 건너뛴다.

```bash
mkdir -p ~/.claude/skills
for skill in token-efficiency-summary loop-spec graph-plan; do
  if [ -e "$HOME/.claude/skills/$skill" ] || [ -L "$HOME/.claude/skills/$skill" ]; then
    printf '기존 설치 보존: %s\n' "$skill"
  else
    cp -R "$skill" "$HOME/.claude/skills/$skill"
  fi
done
```

복사 대신 `ln -s "$PWD/$skill" "$HOME/.claude/skills/$skill"`로 symlink해도 된다. SKILL.md만 복사하지 말고 폴더 전체를 설치한다. Codex 등 다른 호스트는 해당 호스트의 스킬 경로를 사용한다.

- token-efficiency-summary는 Python 3.8+(표준 라이브러리만)가 필요하다. 첫 실행 때 분석기를 `~/.claude/scripts/analyze-tokens.py`로 복사한다.
- loop-spec이 만든 loop는 `/goal`이 있는 Claude Code에서 돌린다. graph-plan이 만든 graph는 subagent나 agent team을 쓸 수 있는 호스트에서 돌린다. 두 스킬 모두 설계만 하고, 요청하지 않으면 실행하지 않는다.
- 분석기 테스트는 설치 없이 저장소 루트에서 돌릴 수 있다.

```bash
python3 -B -m unittest discover -s token-efficiency-summary/scripts/tests
```

## 구성

| 경로 | 내용 |
|---|---|
| [slides/](slides/README.md) | 발표 deck 76장(`index.html`). 공개판이라 발표 노트는 뺐다. |
| `manuals/*.html` | 스킬 3종의 설명서 페이지([token-efficiency-summary](manuals/token-efficiency-summary.html) · [loop-spec](manuals/loop-spec.html) · [graph-plan](manuals/graph-plan.html)). |
| `demos/` | 발표 데모 3종(token · loop · graph)의 결과 페이지. 맨 위 한 화면에 결론과 숫자 셋을 두고, 긴 기록은 접어 두었다. |
| [token-efficiency-summary/](token-efficiency-summary/README.md) · [loop-spec/](loop-spec/README.md) · [graph-plan/](graph-plan/README.md) | 발표에서 사용한 스킬 3종. 각 폴더의 README와 SKILL.md가 사용법이다. |

## 발표에서 사용한 스킬

| 스킬 | 하는 일 |
|---|---|
| [token-efficiency-summary](token-efficiency-summary/README.md) | Claude Code transcript를 읽기 전용으로 분석해 token 사용량, 추정 비용, 낭비 패턴과 권고를 표로 보고한다. |
| [loop-spec](loop-spec/README.md) | "done"을 기계가 판정할 수 있을 때만 LOOP.md, `check.sh`, `/goal` 줄을 쓰고, 판정할 수 없으면 거절하고 대안을 낸다. |
| [graph-plan](graph-plan/README.md) | 과제에 multi-agent graph가 필요한지 4문항으로 판정하고, 필요하면 node·edge 계약·한도·failure route를 담은 GRAPH.md를 쓴다. |

세 폴더는 영어 원본 스킬의 한국어판이다. 명령, 플래그, 파일 이름, stop token(`LOOP-DONE` · `LOOP-CONTINUE` · `LOOP-BLOCKED`), 코드 블록과 스크립트는 원문 그대로 두었다.

## 실험 근거

- **조건**: 모든 비교는 두 arm이 같은 모델(`claude-opus-5-5`, effort xhigh), 같은 repo 커밋, 같은 과제 문장을 쓰고 변수 하나만 바꿨다. 셀당 N=2–3이다. **작은 N의 방향성 결과**이며 통계적으로 유의하지 않다(예: 과장된 "done" 2/3 vs 0/3, p=0.4). 비용은 CLI가 보고한 정가 기준 추정치이며 실제 청구액이 아니다.
- **Token**: 쓰지 않는 MCP·skill·plugin을 빼서 prefix를 줄이자 repo 3개에서 비용이 −36~38% 줄었다. 성공은 21/21로, 잃은 능력은 없었다.
- **Loop**: 품질은 single 프롬프트와 사실상 같았고(쉬운 과제 3/3 vs 3/3, 어려운 과제 1/3 vs 0/3로 차이가 유의하지 않음) 비용은 1.45–1.7배였다. 대신 지표가 흔들리는 과제에서 single은 3번 중 2번 목표를 넘었다고 과장 보고했고, loop의 과장 보고는 0/3이었다. loop가 산 것은 효율이 아니라 정직한 "done"이다.
- **Graph**: PR 리뷰 과제 2개에서 품질은 single과 같고(6/6 vs 6/6, 14/14 vs 14/14) 비용은 2.7–5.5배였다. "lint 다 고쳐" 과제에서는 graph-plan이 스스로 graph가 아니라 loop를 골랐다(2/2). 반대로 한 context를 넘는 실제 서비스 repo 하나의 보안·privacy 감사(API route 121개)에서는 graph가 맞는 발견을 run당 32건, single이 11건 찾았다. 비용은 2.7배, 발견 1건당 비용은 비슷했다(arm마다 2번, [demos/graph.html](demos/graph.html)). 발견 내용은 공개하지 않는다.

## 공유 범위

- 발표자 노트, 발표 원고, 실험 raw transcript와 로그, 내부 데이터, 실험에 쓴 repo clone은 포함하지 않는다.
- `token-efficiency-summary/scripts/analyze-tokens.py`는 로컬 transcript만 읽고 네트워크를 쓰지 않는다. transcript는 읽기 전용으로 열며, 파일은 옵션을 줄 때만 쓴다: `--save-history`의 history 파일, `--dashboard`의 HTML 대시보드.
- `manuals/*.html`은 외부에서 글꼴(Google Fonts: Inter · Noto Sans KR · JetBrains Mono)과 PNG 저장용 라이브러리(jsDelivr의 `html-to-image@1.11.11`)만 불러온다. 페이지는 데이터를 어디에도 보내지 않으며, 스크립트는 복사 버튼·퀴즈·계산기·테마 전환만 브라우저 안에서 처리한다. 본문 링크(Anthropic · Cursor · Cognition 글)는 누를 때만 열린다.
