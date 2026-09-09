# 요구사항 — 사용자가 말한 것 전부

이 문서는 사용자가 세션에서 직접 말한 요구사항만 모은 것입니다. 제가 판단해서 넣은 것은
`(제안)` 으로 표시했습니다. 상태는 **됨 / 부분 / 안 됨** 셋뿐이고, 부분과 안 됨에는
무엇이 남았는지 적었습니다. 다음 사람이 이 문서만 읽고 이어받을 수 있어야 합니다.

작성 시점: 2026-09-09 세션 끝, 2026-09-10 에 0-5·0-8·0-10·0-7 갱신. 아래 **0장**이 바뀐 것과 남은 것의 요약이고,
그 뒤 A~H 장은 원래 명세에 상태만 갱신한 것입니다. 두 장이 어긋나면 0장이 맞습니다.

---

# 0. 2026-09-09 세션 — 지금 상태

## 0-1. 돌리는 법 (바뀜)

```
make dev                                            # 로컬 스택. 기체 4대(런타임 4 + 직결 4)
http://localhost:3100/map.html
PYTHONPATH=. python3 -m unittest discover -s tests  # 83개 (약 50초 — 두 세계 3200틱 포함)
node --test tests/test_map.mjs                      # 21개
PYTHONPATH=. python3 tests/test_two_worlds.py       # 점수판 + 기체별 순환 추적(trace)
```
한 판은 `ROUND_TICKS=5000`(0.2 s/틱, 약 17분; 할렘 왕복 4,040틱 + 여유). `RESTART` 버튼 또는 `curl -X POST localhost:8100/reset`.
화면 검증은 playwright(설치된 Chrome)로 찍었습니다 — `chromium.launch({channel:'chrome'})`.
지도 디버그 핸들 `window.__attache` = `{map, motion, flights, queued, latest, stages}` (읽기 전용).

## 0-2. 순환 (사용자가 못 박은 것, 전부 됨 · 화면 확인)

```
창고 마당 제 자리(4대 한 줄, 41m 간격)에서 상자 6개 적재(1초/개)
 → 경로 신청: 노란 점선 → 판정 → (거절: 붉게 깜빡·흐려짐 → 5.6초 뒤 우회로 재신청) → 초록 깜빡 → 출발
 → 착륙장 도착: 수직 착륙 → 3개 하역(1초/개) → 2개 수거(PICKING UP) → 다음 착륙장 (경로 신청은 하역 중에)
 → 두 번째 착륙장: 같은 절차 → 상자 4개 싣고 창고로(RETURNING TO BASE)
 → 마당 제 자리에 착륙 → 4개 하역 → 다시 6개 적재 → 반복. 배터리 40% 아래면 충전대(이륙장) 먼저.
```
- 배달지는 주소가 아니라 **지정 착륙장 16곳**(`sim/world.py LANDING_AREAS`, 맨해튼 11 · 브루클린 3 · 퀸스 2).
  지도에 청록 원 + "DRONE LANDING AREA" 표지. 전부 착륙 둘레 50m 와 이륙장 왕복 경로를 `tests/test_cycle.py`가 봅니다.
  센트럴파크·리버사이드·미드타운 동쪽·칼슈어츠는 KLGA 0ft 격자 안, 브라이언트파크·매디슨스퀘어는 둘레에
  자리가 없거나 길이 안 나서 뺐습니다.
- 출발은 언제나 지상에서. 승인이 싣는/내리는 도중에 와도 상태를 안 바꾸고, 일이 끝나고 승인 확인
  (`CLEARANCE_TICKS=25`, 화면의 승인 표시 4.4초 + 여유)이 끝나야 뜹니다.
- 공중에서 멈추는 경우는 구역 폐쇄로 회수될 때뿐이고, 그때도 가장 가까운 바깥으로 나간 뒤 바로 재경로
  (`nearest_exit`, 화면 라벨 AWAITING ROUTE). 3200틱 시험에서 공중 대기 2틱.
- 기체 옆 문구: `LOADING 3/6` `UNLOADING 2` `PICKING UP 4` `LANDING` `CHARGING 45%` `RETURNING TO BASE`
  (상태마다 색). 지상에서 승인을 기다릴 때는 아무것도 안 씁니다 — 경로 쪽 글자가 말합니다.
  아랫줄에 `↑ 25 m`(승인된 순항 고도).
- 화면에서 뺀 것(사용자 지시): 돈(기단 지출·$12), 배터리 방전(`grounded`), 모터 진동·자율주행 고장 줄거리,
  "turn back/회항" 표현(→ "route pulled"), AWAITING CLEARANCE 라벨.

## 0-3. 판정 (런타임이 보는 것)

- 판정 함수는 여전히 `attache/core/geo.first_breach` 하나. 이번에 더해진 기준(전부 사용자 지시):
  - **건물 데이터 = 화면이 그리는 건물.** 뉴욕시 공개 데이터(3,284동)에는 없는데 타일(OSM)에는 있는 건물을
    승인된 회랑이 뚫고 갔습니다. `scripts/fetch_tile_buildings.mjs` 가 OpenFreeMap 벡터 타일의 building 레이어
    (`render_height`)를 그대로 뽑습니다 — 20m 이상 34,581동 중 **40m 이상 14,837동**을 씁니다
    (`configs/airspace/nyc_buildings.json`). 40m 아래 건물은 순항 구간을 막지 못하므로(아래 규칙) 뺐고,
    다 넣으면 계획기가 몇 배 느려집니다.
  - **옥상 위 이격 50m**(`VERTICAL_CLEARANCE_M`, 건물 Volume 의 `clearance_m`). 건물 위를 지나려면 옥상에서
    50m 는 떠야 합니다. 유인기 규정(14 CFR 91.119, 장애물 위 500/1,000ft)을 본뜬 운영 규격이고
    Part 107 에는 최소 이격이 없습니다.
  - **순항 고도 90m**(`CRUISE_ALT_M`). 40m 아래 건물은 넘어가고 그보다 높은 건물은 옆으로 돕니다. 25m 로
    해 보니 어떤 건물도 못 넘어 맨해튼에 길이 하나도 안 났습니다.
  - **옆 이격**: 건물 10m(`SEPARATION_M`), 격자·폐쇄 구역 40m(`ZONE_SEPARATION_M`).
  - **착륙 둘레 50m**(`LANDING_SEPARATION_M`, `Airspace.landing_breach`). 경로의 끝점 둘레에 건물·금지 구역이
    있으면 런타임이 거절합니다("착륙 지점 둘레에 건물 — 내려앉을 수 없습니다"). 옆으로 10m 띄운 자리는
    지나갈 수는 있어도 90m 를 수직으로 내려오기엔 탑 사이 골짜기였습니다(Hunters Point 사진).
  - 계획기 `_crosses` 의 구간 고도 = 양 끝 중 낮은 쪽. 도착점 고도로 구간 전체를 보던 탓에
    천장 낮은 칸(KLGA 100ft)에 들어간 기체가 영영 못 나왔습니다(drone-04 가 LIC 에서 80초 정지).
  - 출발점·목적지 격자점이 막혀 있으면 둘레 두 칸에서 열린 격자점을 씁니다(`_free_node_near`).
- 검증 방법: `tests/test_cycle.py` 가 착륙장 16곳 전부 둘레 50m 와 왕복 경로를 확인하고,
  스크래치 `through.mjs`(playwright) 가 승인된 회랑을 6m 마다 화면 타일 건물과 대조합니다 — 이번 결과 0건.
- 뉴저지는 여전히 착륙장이 없습니다. 타일 건물은 뉴저지도 뽑을 수 있으니(bbox 넓히면 됨) 원하면 추가 가능.

## 0-4. 이 세션에서 고친 버그 (원인까지)

| 증상 | 원인 | 고친 곳 |
|---|---|---|
| 승인 경로가 기체 자리가 아닌 딴 데서 시작·가다가 되돌아감·판마다 점점 엉망 | 런타임 배경 스레드가 구역 회수 때 `LedgerEntry` 객체를 JSON 으로 못 보내 죽음 → 그 뒤 옛 위치·옛 판을 계속 내보냄. 로컬 어댑터만 쓰는 시험은 못 잡음 | `runtime/service.py recall_flights`(`entry.id`), `background()` 예외 삼킴, `test_mechanisms` JSON 어댑터 시험 |
| 거절 재생이 기체와 따로 놂 | 공중에서 신청한 경우 기체는 이미 이동 중 | 공중 재경로는 재생 안 함(`stageRoute`), 지상 출발은 hold 로 맞춤 |
| 기체가 회랑 속에 파묻힘 | 8m 두께 리본이 기체 고도에 겹침 | 리본을 기체 4.5m 아래 3m 두께로 |
| 회랑이 건물을 뚫고 감 (1) | Catmull-Rom 곡선이 50m 격자 우회로의 모서리를 잘라냄 | `makeCurve` 를 곧은 구간으로 |
| 회랑이 건물을 뚫고 감 (2) | 선분이 건물 모서리 0.5m 옆을 지나도 판정은 통과 | 이격 거리 판정 |
| 구역 색이 건물·기체를 덮음 | `cells-fill` 이 3D 레이어 뒤에 그려짐 | 공역 레이어를 건물보다 먼저 추가(`addAirspaceLayers`) |
| 착륙장 도착 뒤 영영 "승인 대기" | ① 천장 낮은 칸에서 못 나옴(위) ② 상자 없는 기체가 `decline_job` 으로 새 주문을 받아 빈 채로 비행 ③ 창고에서 `depart` 만 반복(주문 없음) | `_crosses` 고도, `decline_job`/`depart` 가 `stops_left`·주문을 봄, `_free_node_near` |
| 닫힌 구역 안에서 제자리 대기(341틱) | 회수가 제자리 정지라 출발점이 금지 구역 → 어떤 경로도 안 나옴 | 런타임이 `exit` 좌표를 주고 기체가 나감(`nearest_exit`), 6틱 |
| "REJECTED · RESTRICTED" | 이륙장 점유·지시 거절도 공역 거절처럼 표시 | 사유 코드로 문구 분기(`stage.reason`) |
| 이륙장 잠금이 돌아오는 비행 내내 걸려 나머지 3대가 몇 분씩 정지 | 잠금 해제가 `depart` 시점 | 마당 제 자리로 먼저 돌아옴, 잠금은 `fly_route`(실제 출발) 때 해제 |
| 상자를 쌓아도 화면에 안 나옴 | 시뮬레이터에 적재량 개념이 없었음 | `Vehicle.load/stops_left/pickup` |

## 0-5. 제가 정한 것 (사용자가 뒤집을 수 있음)

- 범례(왼쪽 아래)는 산출·승인·거절, 막은 것, 착륙장, 창고·충전대, 격자 등급만 남겼습니다(사용자 지시).
- 협상 표시 시간을 절반으로(그리기 2.4·판정 0.6·붉게 1.6·흐려짐 1.0·초록 1.4초). 사용자: "짐 쌓고 내리는 게 왜 이리 오래".
  상수 셋이 묶여 있습니다: `map-route.mjs` ↔ `world.py CLEARANCE_TICKS` ↔ `loop.py REDRAW_DELAY_S`.
- 상자: 6개 적재, 착륙장마다 3개 하역 + 2개 수거, 창고에서 수거분 하역. 사용자 말("6개, 3개씩 두 곳, 픽업")을 그대로.
- 충전은 마당에 돌아와 40% 아래일 때만 이륙장(충전대)으로. 배달 중 배터리로 돌아서는 규칙은 뺐습니다(항속 60분).
- 지시(감항성 지시)로 막힌 행동은 20초에 한 번만 재신청(`BANNED_RETRY_S`). 매번 내면 화면이 거절로 도배됨.
- 예산(`authority` 한도)은 **화면에서만** 뺐고 런타임 코드에는 남아 있습니다. 사용자 말대로 "기업의 몫"이면
  `configs/fleet.yaml authority` 와 `runtime/authority.py` 를 걷어내야 합니다 — **물어볼 것**.
- 구역 폐쇄 시나리오(응급헬기 회랑, 560~900틱)는 남겼습니다. 사용자가 "가다가 끊겨 되돌아가는 것"을 없애라고
  했는데, 실제 원인은 위 배경 스레드 버그였고, 회수 자체는 런타임의 핵심 주장(도착한 규칙을 즉시 강제)이라
  "가장 가까운 바깥으로 나가서 즉시 재경로"로 바꿨습니다 — **없앨지 물어볼 것**.
- **제품 방향(A안, 2026-09-10)**: 기존 배차(dispatch) 옆에 붙는 셋 — ① 사전 승인 게이트(preflight gate)
  ② 비행별 원장 보고서(`GET /ledger/report`, 0-10) ③ 순응 감시(conformance monitoring: 승인한 창보다 일찍 뜨면
  `nonconforming`, 나는 중 도착한 규칙은 회수). 배차를 대신하지 않고 검증만 합니다. 표준 위치는 **ASTM F3269
  (run-time assurance)의 한 사례를 배차 층에 둔 것** — 판정 코드가 안전 감시기, 모델·에이전트가 비검증 구성요소 —
  이고, 의도는 **F3548 식 4D 의도**(회랑 + 시간 창)로 적습니다. 두 표준이 정하지 않은 자리를 채웁니다:
  에이전트가 권한자에게 **신청하는 접점**(양식·값으로 된 거절·해결 사다리)과 **출처 기록**(누가 그렸고 어느 검사가
  잡았는지). A안을 고른 이유는 런타임이 계획·지출·판단을 하지 않는다는 원칙과 맞고 기존 배차를 갈아엎지 않아
  붙일 자리가 있기 때문입니다 — **사용자 확인 필요**.

## 0-6. 눈으로 확인 못 한 것

- 한국어(KR) 화면 문구 전체. 새 문구는 넣었지만 KR 로 찍어 보지 않았습니다.
- 감항성 지시(1050~1350틱) 구간의 화면 — dv-x500 두 대가 마당/착륙장에서 60초 서 있는 모습.
- 충전대 경쟁(4대 중 둘이 동시에 40% 아래) 장면.
- 모바일/좁은 화면.

## 0-8. 모델 (2026-09-09 밤 세션 — Nemotron 이 실제로 무엇을 하나)

**모델은 세 자리에서 쓰이고, 세 자리 모두 결정권이 없습니다.** 모델이 내놓는 것은 전부 양식이고,
양식이 아니면 버리고 규칙이 대신합니다. 판정 함수(`attache/core/geo.first_breach`, `landing_breach`,
런타임 검사)는 모델 코드가 건드리지 않습니다. 런타임 코드는 `drafter` 라는 글자를 읽지 않습니다
(`tests/test_drafter.py RuntimeNeverReadsTheDrafterTest` 가 grep 으로 못박음).

| 자리 | 티어 | 모델이 내는 것 | 코드가 하는 것 | 못 하면 |
|---|---|---|---|---|
| 신청서 (`attache/agent/propose.py`) | nano (급하면 super) | `{"action","pad","rationale"}` | 행동 목록·패드 이름 검사 | `by_rule` 이 씀 |
| **경로 초안** (`attache/agent/drafter.py`, 새로 넣음) | nano | `{"legs":[{lat,lon,alt_m}…]}` | 양식·12구간·서비스 상자·고도 40~120·양 끝 고정·길이 2.5배·운영사 고도 규칙·운영사 사본으로 `first_breach` → 한 번 더 묻기 | A* (`OperatorPlanner.draw`) |
| 중재 (`attache/runtime/arbiter.py`) | ultra | `{"choice": n, "reason": "…"}` (번호만도 됨) | 번호 범위 검사, 이유 140자 → `decision.detail.arbiter_reason` | `by_rule` (영향 범위 > 배터리 > 순서) |

### 경로 초안의 흐름 (사용자 지시: "드론이 sLLM 으로 경로를 고르되 항상 런타임 허가 아래")

```
지상에서 직선 신청 → 런타임 거절(airspace) → 그 순간 작업 스레드에서 시작(화면의 거절 표시 5.6초 REDRAW_DELAY_S 는 따로 흐름)
  → nano 에게 지도 읽기와 함께 초안 요청 (원점·목적지·규칙 한 문단·직선이 차례로 부딪히는 것과
    거리·어느 쪽이 열려 있는지·천장이 낮아지는 구간·런타임의 거절 사유)
  → 코드 검사 → 운영사 고도 규칙(구간마다 가장 낮은 안전 고도, planner.straight 와 같은 규칙)
  → 운영사 사본으로 first_breach → 걸리면 걸린 것 전부를 적어 한 번 더
  → 두 번 안 되면(또는 거절 시각부터 DRAFT_TIMEOUT_S 60초가 다 되면) A* → 어느 쪽이든 런타임이 다시 판정
공중 재경로(회수)는 모델에게 묻지 않고 A* 로 바로 냅니다 — 떠 있는 초가 아깝습니다.
```

신청서마다 `params.drafter` = `"straight"` | `"nano:<모델 id>"` | `"astar"`, `params.draft_attempts` = 모델에게 물은 횟수.
원장과 `/state` 에 그대로 남고 화면은 그 값으로 "Route by nano / A*" 를 씁니다. 판정은 이 값을 보지 않습니다.
`tests/test_two_worlds.py GuardedSide` 는 `loop.py` 의 이 흐름을 그대로 베낀 것이라(의도적 중복) 시험이
stub/fixture/chaos 초안기를 꽂을 수 있습니다.

### 실제로 재 본 것 (Ollama, Mac, nemotron-3-nano 30B-A3B q4_K_M 24GB)

- **Ollama `/v1/chat/completions` 는 생각(thinking)을 기본으로 켭니다.** 답은 `message.content`, 생각은
  `message.reasoning` 에 따로 옵니다. 사소한 질문도 첫 호출 12.8초(157 토큰 생각 + 적재).
  `"reasoning_effort":"none"` 을 보내면 생각 없이 답합니다(1.1초). `"think": false` 는 `/v1` 에서 무시되고
  원생 `/api/chat` 에서만 먹습니다(0.4초). `response_format: {type: json_object}` 는 받습니다.
  → `.env.example` 의 Ollama 블록은 `LLM_REQUEST_EXTRA={"reasoning_effort":"none"}`.
- **클라이언트(`attache/llm/client.py`)**: `content` → `reasoning_content` → `reasoning` 순으로 답을 찾고, 앞머리
  `<think>…</think>` 는 떼며, 닫히지 않은 `<think>` 는 전부 생각으로 봅니다. 생각 속에만 있는 JSON 은 답이
  아닙니다. `json_object=True` 면 `response_format` 을 붙이고 서버가 400 으로 거절하면 빼고 한 번만 다시 냅니다
  (타임아웃은 다시 안 냄). `LLM_TIMEOUT_S`(런타임 20, 기체 6 — `loop.build_llm`), `LLM_REQUEST_EXTRA`(JSON, 요청에
  섞음), `LLM_RECORD_DIR`(호출마다 `{tier, model, system, user, text, via, latency_ms}` 파일). 답은
  `LlmReply(text, model, latency_ms, via)`.
- **지연 (생각 끔, 이 Mac)**: 신청서 0.5~1.4초(첫 호출 3.1초), 중재 1.3~1.4초, **경로 초안 한 번에 5~19초**
  (700 토큰 한도, 6구간 안팎; 한가한 서버에서 같은 프롬프트 재생 8.7~8.9초), 두 번 물으면 8~30초. 기체 공통
  타임아웃(6초)으로는 초안이 전부 잘려 첫 라이브(15건)에서 nano 가 그린 경로가 0건이었습니다. 그래서 초안 호출은
  자기 예산을 듭니다 — `DRAFT_TIMEOUT_S`(기본 30, `drafter.py`) — 신청서·중재는 그대로 6/20초. 서버가 방금
  타임아웃했으면 `DRAFT_BACKOFF_S`(기본 30) 동안은 묻지 않고 A* 로 갑니다(끝나지 못할 호출 뒤에 기체를 세우지
  않으려고). 라이브 측정값은 아래 표.
- **nano 의 초안 성적 (녹음 18건)**: 창고 → 센트럴파크 북쪽·모닝사이드·피어76·이스트메도 같은 **10km 급 맨해튼
  횡단은 0/8** — 윌리엄스버그 탑을 0~9m 로 스치거나 0ft 격자 옆을 1m 로 지납니다. 5자리 위경도로 14,837동을
  피하는 것은 이 크기 모델이 글로 할 수 있는 일이 아닙니다. **강 건너 짧은 구간은 3/10** (콜리어스훅 2/2,
  이스트리버파크 1/2, 브루클린브리지파크 0/4, 거버너스 0/2). 실패는 전부 운영사 사전 판정에서 잡혀 A* 로
  넘어갔고, 그것이 이 설계가 말하는 바입니다 — 누가 그리든 보장은 같습니다.
- **Nebius Token Factory**: 키가 없어 이 세션에서 한 번도 못 불렀습니다. `configs/fleet.yaml` 의 id 는
  super `nvidia/nemotron-3-super-120b-a12b`(확인됨), ultra `nvidia/Nemotron-3-Ultra-550b-a55b`(대소문자 미확인),
  nano `nvidia/Nemotron-3_5-Lightning`(목록에 30B Nano 가 없을 때의 자리). `python3 scripts/llm_probe.py` 가
  GET /v1/models 로 실제 목록을 보여주고 티어마다 양식 하나씩 물어 id/via/지연/파싱 결과를 찍습니다(하나라도
  못 쓰면 exit 1). 예전 id(Super 100B-A10B)는 존재하지 않아 전부 갈았습니다(compose.yaml, robot.yaml 포함).

### 돌리는 법

```
# Ollama 로컬 — 드론마다 서버 하나 (아래 "드론마다 서버 하나" 절. 이 Mac 의 기준 실행법)
ollama pull nemotron-3-nano:4b                 # 기체용 4B(2.8 GB). 런타임 대역은 nemotron-3-nano(:latest, 30B, 24 GB)
scripts/ollama_fleet.sh start 4                # 11435..11438 에 서버 4개, 4B 를 하나씩 데움. status 4 · stop 4
export LLM_PER_ASSET_URLS="http://127.0.0.1:11435/v1 http://127.0.0.1:11436/v1 http://127.0.0.1:11437/v1 http://127.0.0.1:11438/v1"
LLM_BASE_URL=http://localhost:11434/v1 NEBIUS_API_KEY=ollama \
LLM_REQUEST_EXTRA='{"reasoning_effort":"none"}' LLM_RECORD_DIR=.run/llm ./scripts/dev.sh
# → 기체 i 는 i 번째 URL(nano=nemotron-3-nano:4b), 런타임은 11434(super 대역=nemotron-3-nano:latest, 기체에는 안 감)
# Ollama 로컬 — 서버 하나 (예전 방식. 초안이 줄을 서서 잘림, 아래 라이브 1)
ollama pull nemotron-3-nano
LLM_BASE_URL=http://localhost:11434/v1 NEBIUS_API_KEY=ollama \
MODEL_NANO=nemotron-3-nano MODEL_SUPER=nemotron-3-nano MODEL_ULTRA=nemotron-3-nano \
LLM_REQUEST_EXTRA='{"reasoning_effort":"none"}' LLM_RECORD_DIR=.run/llm ./scripts/dev.sh   # 초안 예산은 DRAFT_TIMEOUT_S(기본 30)
# Nebius
NEBIUS_API_KEY=… LLM_BASE_URL=https://api.tokenfactory.nebius.com/v1 ./scripts/dev.sh   # id 는 fleet.yaml 기본값
```
`scripts/dev.sh` 는 `.env` 가 있으면 읽고(환경이 우선, 값을 감싼 따옴표 한 쌍은 벗김) `LLM_*`/`MODEL_*`/`NEBIUS_API_KEY`
를 기체·런타임 프로세스에 넘깁니다. `LLM_PER_ASSET_URLS`(공백 구분)가 있으면 런타임 기체 i 에 i 번째 URL 을 주고, 그 URL 이
Ollama(`:1143x`)이고 `MODEL_NANO` 가 비어 있으면 `nemotron-3-nano:4b`, `MODEL_SUPER` 가 비어 있으면 **런타임에만**
`nemotron-3-nano:latest` 를 줍니다. URL 이 기체 수보다 적으면 나머지 기체는 `LLM_BASE_URL` 에 `MODEL_NANO` 없이(초안 없음)
— 문서화 안 된 폴백. 아무것도 없으면 예전 그대로(규칙만). 직결 에이전트는 `DIRECT_LLM=1` 일 때만 모델을 씁니다. 헤더 한 줄 `Nemotron nano · via ollama` 는 `/state.llm` = `{enabled, models, host: ollama|nebius|other|none,
calls: {tier: {ok, fallback, last_ms}}}` 에서 옵니다(런타임 자신의 호출, 즉 중재만 셉니다).

### 중재 스레드

`_settle_contended` 는 이제 자기 데몬 스레드(0.25초)에서 돕니다. 세계 갱신(`_pull_world`)과 한 스레드에 있으면
Ultra 가 생각하는 동안 틱·위치·공지가 그만큼 낡은 채로 판정됐습니다. `tests/test_locks_and_arbiter.py` 가
3초 느린 중재 동안 틱이 계속 오르는지 봅니다. 중재 프롬프트에서 돈·예산 문구를 뺐습니다(안전 검사는 이미
끝났고 남은 것은 순서뿐).

### 시험과 fixture

- `tests/fixtures/llm/drafts_nano.json` — 진짜 nano 초안 10건(통과 3, 거절 7, `expect` 로 표시, `seed`·`start`·`goal`).
  `forms_nano.json` — 신청서 4건 + 중재 1건. `tests/fixture_llm.py FixtureLlm` 이 티어 + 바늘(needle, 프롬프트 부분
  문자열)로 답하고 없으면 None(규칙 차례). `LLM_RECORD_DIR` 파일에 `needle` 만 붙이면 fixture 가 됩니다.
- `tests/test_llm_client.py`(19) 클라이언트·fixture·녹음 신청서, `tests/test_drafter.py`(16) 검사·흐름·녹음 초안·grep,
  `tests/test_locks_and_arbiter.py`(+7) JSON 중재·느린 중재, `tests/test_two_worlds.py`:
  `ChaosDraftsNeverFlyTest` — 건물 관통·0ft 진입·5000m·상자 밖·13구간·쓰레기·생각 속 JSON 을 내는 모델을
  **운영사 사전 판정을 일부러 뺀 초안기(RecklessDrafter)** 에 꽂아 1500틱: `airspace_violations == 0`,
  `ceiling_breaches == 0`, 조종장치 문턱에서 실행 순간 재판정(`JudgingAdapter.unjudged == 0`), 원장의 done
  경로는 전부 `auto`. `RecordedNanoDraftsFlyTest` — seed 2(drone-04 → 콜리어스훅)에서 녹음된 nano 초안이
  승인·실행되고 원장 `params.drafter == "nano:nemotron-3-nano"`.
- 런타임은 숫자가 아닌 legs 를 받으면 판정 함수에 넣지 않고 "경로 양식이 아닙니다" 로 거절합니다(혼돈 시험이
  요청 하나를 500 으로 죽이던 것). 유한하기만 한 값도 양식이 아닙니다: 음수 고도(모든 구역의 '아래' 로 빠져
  건물을 관통하던 것 — 판정 자체도 이제 땅 밑을 땅으로 봅니다, `geo.ground_clamped`), 지구 밖 좌표, 50km 를
  넘는 구간(색인 격자를 1e10 칸 돌며 판정이 영영 안 끝나던 것 — `geo.MAX_LEG_CELLS` 가 마지막 방어선).
  `tests/test_mechanisms.py RouteFormTest`.
- 실행 직전에 다시 판정합니다(`Runtime._rejudge`): 사람 승인을 기다리거나 자원 줄에 서 있는 동안 구역이 닫히면
  승인·배정은 그 경로를 살리지 못하고 `airspace` 로 거절됩니다. `RejudgeBeforeCommitTest`.

### 라이브 1 — 서버 하나 (2026-09-09, 30B 하나를 기체 4대가 나눔, 약 3분)

| 호출 | 건수 | 결과 |
|---|---|---|
| 신청서(form, nano) | 52 | 38 성공 · 중간값 2.0 s · 나머지는 6 s 타임아웃 → 규칙 폴백 |
| 경로 초안(draft, nano) | 9 | 2 완료(19 s, 28 s) · 7 타임아웃(30 s) |
| nano 초안이 승인된 경로 | 0 | 완료된 둘 다 74–81 m 건물을 관통해 `airspace` 거절 → A* 재초안이 승인 |

원인은 모델이 아니라 슬롯입니다. Ollama 가 이 모델(30B-A3B, Mamba 혼합)에는 동시 처리 1 을 강제해서 네 에이전트가
줄을 서고, 뒤에 선 셋이 타임아웃을 맞습니다. 초안 예산은 30 → 60 s 로 올렸습니다(`DRAFT_TIMEOUT_S`). 거절 순간에
초안을 시작하는 것과 드론마다 작은 모델을 따로 띄우는 것은 아래 절에서 했습니다.
데모의 기준 배선은 Nebius Token Factory 입니다 — 슬롯 제한이 없고 출품 요건이기도 합니다.

### 드론마다 서버 하나, 거절 순간에 초안 (2026-09-10)

라이브 1 의 원인(슬롯 1개)을 둘로 풀었습니다. 판정 코드는 손대지 않았습니다.

- **초안은 거절이 오는 순간 시작**합니다(`loop.py`: 거절 → 데몬 스레드 하나에 `drafter.draft(…, deadline)` → 화면의
  거절 표시 5.6 s(`REDRAW_DELAY_S`)는 그대로 흘려보내고 → 거둡니다). 예산 `DRAFT_TIMEOUT_S`(60 s)는 **거절 시각부터
  두 질문을 합쳐** 셉니다 — 초안기가 질문마다 남은 예산으로 자르고, 2 s(`MIN_ASK_S`) 아래면 묻지 않습니다(그래서
  `DRAFT_TIMEOUT_S` < 2 는 초안을 조용히 끕니다). 기체마다 초안 하나만 떠 있고, 초안이 도는 중에 온 거절은 A*
  (attempts 0)로 갑니다. 거절→재신청 사이는 이제 max(예산, 5.6 s) + 수 ms 이지 5.6 s + 지연이 아닙니다. 출처
  (`params.drafter`, `draft_attempts`)와 표시 시간 삼총사(CLEARANCE_TICKS 25 ↔ REDRAW_DELAY_S 5.6 ↔ map-route
  GROW/CHECK/HOLD/FADE/APPROVED_HOLD)는 그대로입니다. 초안을 거두는 사이 기체가 움직였으면 첫 점을 다시 붙이고
  (≤30 m), 더 갔으면 A*, 떠 있으면 버립니다(옛 첫 점을 그대로 내 "첫 점이 기체 자리에서 70m" 거절이 있었음).
- **거절 뒤 두 번째 질문**은 걸린 것을 값으로 말합니다: 장애물 id·이름·옥상 높이·필요 고도(옥상 + 50.5 m 올림,
  `Router.leg_altitude` 와 같은 산수)·그 자리 천장을 넘는지("MUST fly around")·**지날 쪽 하나**(좌표 포함, 이웃
  장애물끼리 같은 쪽 유지 — 옛 30B 녹음이 "왼쪽 80 m / 오른쪽 80 m" 를 번갈아 골라 건물을 관통했음). 첫 질문은
  직선 위에서 어느 고도로도 못 넘는 것 전부를 "GO AROUND" 로 앞에 둡니다 — 천장 칸(KLGA 300 ft) 안의 건물은
  그 칸 천장 − 1 m 로 다시 훑어 찾습니다(처음엔 120 m 로만 훑어 놓쳤고 맥캐런 초안이 그래서 지그재그).
- **드론마다 Ollama 서버 하나**(`scripts/ollama_fleet.sh start 4` → 11435..11438, `nemotron-3-nano:4b`, 문맥 8192 =
  3.0 GB 씩. Ollama 기본 256k 문맥이면 4B 하나가 8.4 GB). 런타임은 11434 를 그대로 쓰고 `MODEL_SUPER` 가 비어 있으면
  `nemotron-3-nano:latest`(30B)를 **Super 대역(stand-in)** 으로 받습니다 — 런타임 프로세스에만. 기체 프로세스는
  `MODEL_SUPER` 를 비웁니다(급한 신청서가 자기 4B 서버에 30B 를 올리던 누수). 진짜 Super 는 Nebius
  `nvidia/nemotron-3-super-120b-a12b` 이고 이 Mac 에는 키가 없어 한 번도 안 불렀습니다.
- 그 밖에: 클라이언트 통계·녹음 번호에 잠금(작업 스레드와 본 루프가 한 `TieredLlm` 을 나눔 — 녹음 파일이 서로
  덮어쓸 수 있었음, `host_of` 가 11434~11439 를 ollama 로 봄); `alt_ma`/`altitude_m`/`altitude`/`alt` 를 고도 키로
  받음(4B 답 10건 중 4건이 `alt_ma`, 값 검사는 그대로); 남은 예산이 첫 질문의 지연보다 짧으면 재질문 생략;
  `propose.possible_now` — 마당에 선 기체가 `charge` 를 적어 런타임이 승인하고 조종장치가 "not on a pad" 로
  거절하기를 6분에 61번 반복하던 것(운영사 쪽 양식 검사, 판정은 그대로. 수정 후 0).

#### 라이브 2 — 4 × 4B 함대 + 30B 대역 (2026-09-10, 이 Mac)

| | 수정 전 첫 7분 (01:24–01:31) | 같은 스택 65분 (01:24–02:29) | 수정 후 10분 (03:14–03:23, 판 하나 2,856틱) |
|---|---|---|---|
| 신청서(nano 4B) | 244건 · 타임아웃 0 · p50 1.2 s · p90 2.6 s · 최대 4.7 s | 2,432건 · 무응답 18 · p50 1.8 s | 미측정 |
| 경로 초안 호출 | 10(첫 6 + 재 4) · 타임아웃 0 · p50 13.5 s · 최대 28.5 s | 90 · 무응답 13 · p50 20.9 s · p90 33.8 s · 최대 56.3 s | 20 · 무응답 1 · p50 29.1 s · 최대 45.2 s |
| 거절→초안 거둠 | 13.5~43.3 s, 전부 예산 60 s 안. 5.6 s 안에 끝난 호출 0 | 미측정 | 미측정 |
| 첫 질문 결과 | 통과 2 · `alt_ma` 2 · 사전 판정 걸림 2(100~164 m 건물) | 재질문 37건으로 승인된 경로 **0** | 미측정 |
| **nano 초안이 승인·실행된 경로** | **2** (drone-02 는 교차 거절 뒤 +30 m 로 승인, drone-04 6구간) | **9** (신청 11 · 초안 10 · 거절 2 = 교차 1, 옛 첫 점 70 m 1) · 건물·구역 위반 0 | **6** (교차 거절 1 → 사다리로 승인) |
| 실행된 경로를 누가 그렸나 | nano 2 · A* 5 · 직선 11 | nano 9 · A* 33 · 직선 19 | nano 6 · A* 10 · 직선 6 |
| 어느 기체가 | drone-02, 04 (브루클린, 3~6구간) | drone-02 8 · drone-04 3 · **drone-01/03(맨해튼 10 km 횡단) 0** | 미측정 |
| 런타임 세계 점수판 | 위반 전부 0 (배달 4) | 판마다 위반 전부 0 (배달 6~8) | 위반 전부 0 (배달 11) |
| 직결 세계 | 천장 6 · 구역 5 · 무기록 14 · 분리 4 (배달 6) | 천장 15~16 · 구역 16 · 무기록 24~27 · 분리 5 (배달 12~15) | 11 · 13 · 24 · 5 (보고된 순서대로, 배달 12) |
| Super 대역(30B, 런타임) | 12 호출 전부 답, p50 3.0 s | 권고 189 · p50 4.8 s · 무응답 13 / 공지 4 중 1 읽음(3 은 20 s 타임아웃) | 공지 8.8 s 에 읽어 보류 · 첫 권고가 `source: super` |

- 솔직하게: 4B 는 **브루클린 짧은 경로(3~6구간)만** 그립니다. 창고→맨해튼 10 km 횡단은 65분 동안 전부 A* 였고,
  거절 사유를 주고 다시 묻는 재질문은 그 판에서 승인된 경로를 하나도 못 냈습니다(GO AROUND 수정 뒤 판은 미측정).
  그래도 nano 가 그린 경로 하나하나가 같은 판정(건물·이격·격자·의도·양 끝)을 지나 실행됐고, 거절된 둘은 판정이
  옳았습니다(남의 회랑 교차, 20 s 초안 사이 70 m 움직인 기체).
- 65분 스택은 마지막 편집(`service.py`·`notices.py`·`client.py`) 이전 프로세스였고 세션 중간에 sim `/reset` 이 밖에서
  한 번 들어와(≈01:29:45) 점수판이 판 단위로 끊깁니다. 공지·권고 수치(0-10)는 수정 후 판 것만 믿으면 됩니다.
- 셀 때: 원장은 같은 id 로 두 줄(pending → 확정)이 남으니 id 로 중복을 걷어야 하고, 점수판은 sim `/compare` 에
  있지 런타임 `/state` 에는 없습니다. 판 하나는 dev.sh 틱 속도에서 약 15분.
- fixture: `drafts_nano.json` 에 실주행 4B 통과 초안 둘(맥캐런, 브루클린브리지파크→창고, seed 7)이 들어가
  `RecordedNanoDraftsFlyTest` 가 skip 없이 돕니다(원장 drafter 는 fixture 모델명 `nano:nemotron-3-nano`; 벽시계에
  기대던 flaky 는 `backoff_s=0` 으로 고침).

## 0-9. 분리·의도·공지 (2026-09-09 — 경로 사이의 판정)

지금까지 판정은 경로 하나와 공역 사이였습니다. 이 절은 경로와 경로 사이, 그리고 문장으로 도착하는
공역입니다. 원칙은 그대로입니다 — 런타임은 **검증만** 하고(경로도 시각도 대신 정하지 않음), 모델은
어느 검사에도 손대지 않으며, 직결 세계는 같은 감지기·같은 신청서 작성기를 쓰고 그저 묻지 않을 뿐입니다.

### 규칙과 숫자

| 규칙 | 숫자 | 근거 | 어디에 |
|---|---|---|---|
| 수직 구간 판정 | 5m 마다 표본 | 건물 띠(옥상+이격 50m)가 5m 보다 좁을 수 없음 | `geo.vertical_column`, `service.check_route` "columns" |
| 의도(4D) 회랑 | 옆 **30m + 항법 오차 10m**, 위아래 **±(25m + 한 틱 승강 1.6m)**, 시간 **±30틱**(24초) | 수평 30m·수직 25m 는 EU U-space CORUS 와 NASA UTM TCL 시연이 소형 무인기에 쓴 분리 규모(유인기 3NM/1,000ft 를 기체 크기·속도로 줄인 값). F3548 은 의도 부피에 운영사의 항법·순응 오차가 들어 있기를 기대함 — 최소치만으로 그리면 31m 옆의 두 승인이 경유점 반경(6m) 만큼 모서리를 자르는 것으로 분리를 잃음. ±30틱은 승인 확인(25틱)과 적재 오차를 덮는 여유 = 순항 530m | `runtime/intents.py corridor_widths`, `TRAFFIC_LATERAL_M/TRAFFIC_VERTICAL_M`(geo), `performance.nav_tolerance_m`(fleet.yaml ≥ sim ARRIVAL_RADIUS_M), `TIME_PAD_TICKS` |
| 떠 있는 기체의 자리 | 의도가 끝난(회수·물림·반려) 떠 있는 기체는 나가는 길 + 끝없는 기둥(contingency)으로, 등록부에 없이 떠 있는 기체는 지금 자리의 기둥(presence)으로 판정에 들어감 | 떠 있는 기체는 언제나 어딘가에 있음. 의도가 없다고 빠지면 그 자리를 지나는 다음 신청이 승인됨 | `intents.hold`, `service._others`, `_end_intent` |
| 경로의 양 끝 | 첫 점은 기체 자리에서, 끝점은 목적지(배달지·이륙장)에서 30m 안 | 조종장치는 첫 점을 버리고 지금 자리에서 날고, 끝점 다음은 배달지까지 판정 없이 이어 감 — 딴 데 적은 첫 점은 판정한 길과 나는 길을 갈라놓음 | `service._endpoint_problem`, 검사 이름 `endpoints` |
| 출발 순응 | 승인한 출발 틱 − 30틱보다 일찍 뜨면 의도를 실제 출발로 옮기고(reanchor) 원장에 `nonconforming` | 조종장치가 미룬 출발을 안 지키면 판정이 빈 하늘을 막고 실제 기체는 어느 창에도 없음 | `IntentRegistry.observe`, `service._ledger_nonconformance`; sim 은 땅의 모든 상태에서 `depart_after` 를 지킴(`ready` 를 거침) |
| 충돌 정의 | 공간 **그리고** 시간이 겹칠 때만, 1cm 보다 떨어지면 무관 | ASTM F3548-21 §3.2.8 (operational intent, conflict) | `intents.first_conflict` |
| 우선순위 | 먼저 낸 쪽. 예외: 떠 있는 기체의 비상 재신청은 아직 안 뜬 상대를 물림(`withdrawn`). 물림은 검사가 아니라 **실행의 일부** — 재신청이 실제로 나간 뒤(`_on_committed`)에만 상대가 물리고, 사람 보류·한도·조종장치 실패로 안 나간 재신청은 아무도 물리지 않음 | F3548 의 contingent 우선. 땅에 있는 쪽이 다시 내는 것이 하늘에서 기다리는 것보다 쌈 | `service._check_traffic`(고르기만: params.withdraw), `_on_committed` → `_withdraw` |
| 착륙장 | 살아 있는 다른 의도가 내리는 자리(30m)에는 앞뒤 없이 못 내림(내린 기체는 다음 승인까지 거기 있음), 아직 안 뜬 출발점도 뜰 때까지, 그리고 텔레메트리로 땅에 서 있는 기체의 자리도 — 그 기체의 승인된 출발이 내 도착보다 앞이면 됨 | 한 착륙장에 두 대는 없음. 착륙 기둥 ±30틱만 막으면 그 뒤에 도착하는 신청이 아직 상자를 내리는 기체 위로 내림 | `intents.landing_conflict`, `intents.ground_conflict`, `service._occupants` |
| 분리 상실 계측 | 두 기체가 같은 틱에 수평 30m·수직 25m 안 → 쌍마다 1회. 떠 있는 기체가 땅에 선 기체의 그 안에 들면 `site_conflicts` | 판정과 같은 숫자여야 계측이 판정을 말함 | `sim/world.py _detect_separation_losses`, 점수판 `separation_losses`·`site_conflicts` |
| 신고 성능 | 순항 22m/s·상승 2·하강 1.75·틱 0.8초·승인 확인 25틱 | 운영사가 시간 창을 직접 적으면 좁게 적어 충돌을 숨길 수 있음 → 런타임이 경로에서 셈함 | `configs/fleet.yaml performance`, `tests/test_intents.ScheduleTest` 가 sim 상수와 대조 |
| NOTAM 문법 | `AREA BOUNDED BY DDMMSS[NS]DDDMMSS[EW]…` / `<r>NM RADIUS OF <좌표>` / `SFC-400FT AGL` / `0907-0912Z` 또는 `TICK a-b` | FAA NOTAM 서식. 틱 0 = 0900Z(`clock_epoch_z`) | `core/notam.py parse_notice` |
| 모델이 읽은 공지 검사 | 꼭짓점 3~32, 서비스 상자 안, 넓이 ≤ 4km², 바닥 0~121.9m, 천장 null 또는 ≤ 1,524m | 모델이 지어낼 수 있는 것의 상한. 이 안이어도 사람이 확인해야 걸림 | `core/notam.validate`, `runtime/notices.py` |

### 흐름

```
신청(legs) → 양식 → 경로(first_breach) → 수직 구간(이륙 기둥·꼭짓점 승강·착륙 기둥) → 착륙 둘레
  → 의도 계산(신고 성능으로 구간마다 진입·이탈 틱) → 다른 기체의 살아 있는 의도·떠 있는 자리와 4D 교차
  → 착륙장 점유(의도 + 서 있는 기체) → 정책·한도 → 실행(여기서 상대 물림) → 의도 등록(accepted)
  → 텔레메트리로 activated(승인한 창보다 일찍이면 reanchor + nonconforming) → 내리면 ended
  → 회수·물림·반려로 끝났는데 떠 있으면 contingency(나가는 길 + 끝없는 기둥)가 뒤를 이음
교차 거절(code airspace, policy_hit traffic, params.blocked_kind traffic|landing, blocked_asset,
  blocked_at, blocked_leg, blocked_until_tick) → 운영사 사다리: 같은 길 +30m(resolution altitude)
  → 상대 회랑이 비는 틱까지 출발 지연(resolution delay, holding_for, depart_after_tick; 최대 3번)
  → A* 재작성 → 이번 차례는 접음(다음 차례에 처음부터). 떠 있으면 지연은 없음(공중 정지가 되므로).
공지(text) → 문법 → 그 틱에 공역에 넣고 날던 경로 회수 → 창이 닫히면 뺌
  → 문법이 못 읽으면 Super 가 같은 스키마로 구조화 → 검사 → 승인 화면(action publish_notice) 보류
  → 사람이 확인하면 source "human" 으로 걸림. 모델이 읽은 것은 사람 없이 절대 안 걸림.
```

### 화면·기록에 노출되는 값 (ui 는 소유자가 맞춤)

- `/state`: `intents: [{asset, state, id, kind: route|contingency, from_tick, to_tick, …}]` — contingency 의
  `to_tick` 은 `OPEN_ENDED_TICK`(10^9)+30 이라 화면은 끝없는 것으로 그려야 합니다. `notices: [{id, name, kind, from_tick,
  until_tick, source: grammar|human|structured, polygon, floor_m, ceiling_m, text}]`, `awaiting_human` 에
  `publish_notice` 항목(모델이 읽은 공지). 배너는 `notices` 에서 그려야 합니다 — sim `/compare` 의
  `bulletins` 는 원문(text) 뿐입니다.
- 텔레메트리: `holding_for`(미룬 출발 동안 누구를 기다리는지), `/telemetry/{asset}` 에 `tick`.
- 원장 항목 `context`: `{tick, airspace_revision, policies, intent_id, checks_run}`. 중복 거절도 남습니다.
  런타임이 쓴 결정 코드: `withdrawn`(비상 재신청에 자리를 내줌), `nonconforming`(승인한 창보다 일찍 뜸,
  action `conformance`, outcome `noted`), `notice_published`/`notice_refused`/`notice_unreadable`.
  양 끝 거절은 code `airspace` 에 params `blocked_kind origin|destination`, `blocked_gap_m`(blocked_volume 없음).
  실행된 재신청의 닫는 줄에는 `params.withdrew`·`context.withdrew`(물린 기체 목록).
- 점수판: `site_conflicts`(떠 있는 기체가 땅에 선 기체의 30m·25m 안). 런타임 세계는 0 이어야 합니다.
- 직결 에이전트(`direct_agent/loop.py`)는 하네스의 DirectSide 처럼 배달지까지의 직선을 `legs` 로 붙여 보냅니다 —
  경로 없는 fly_route 는 조종장치가 `ready` 에서 띄우지 않아, 라이브 직결 세계가 한 번도 뜨지 않았습니다.

### 씨앗 7 한 판(4000틱)에서

- `OPENING_STOPS` 에 02(맥캐런)·04(콜리어스 훅)를 더해 자리 60m 북쪽에서 직선이 교차합니다. 직결 세계는
  같은 틱에 뜬 두 대가 같은 순간 그 점을 지나 분리를 잃고(`separation_losses > 0`), 런타임 세계는 0 —
  `tests/test_two_worlds.py` 가 못박고 `python3 tests/test_two_worlds.py` 가 `runtime` 항목에
  교차 거절·해결(altitude/delay)·물림 수를 찍습니다.
- 구역 공지는 `ZONE_TEXT`(FAA 문장, 0907-0912Z = 525~900틱)로만 나갑니다. 시뮬레이터는 폴리곤을 주지
  않고, 런타임이 문법으로 읽어 만듭니다(`test_notam`).

### 제가 정한 것 (뒤집을 수 있음)

- 지연 재신청의 `depart_after_tick` 은 상대 **의도 전체**가 아니라 **겹친 부피**가 비는 틱입니다.
  전체를 기다리면 교차 지점을 60초에 지나가는 상대를 10분 기다립니다. 거절이 그 틱을 `blocked_until_tick`
  으로 알려 줍니다.
- 착륙장 점유는 의도와 텔레메트리 둘 다 봅니다. 살아 있는 의도가 내리는 자리는 상대가 다음 승인을
  받을 때까지 끝없이(앞뒤 없이) 잡고, 이미 내려서 하역 중인 기체(의도 ended)는 땅에 서 있는 자리로
  잡습니다. 비는 틱은 상대의 다음 승인이 있어야 알 수 있어서, 거절의 `blocked_until_tick` 은 가장 이른
  가능성(상대 착륙 기둥 끝 또는 지금 + 30틱)일 뿐입니다 — 지연 사다리는 대개 접히고 다음 차례에 다시 냅니다.
- 떠 있는 기체가 **떠 있는** 상대와 겹치면 거절합니다(물릴 수 없으므로). 그 운영사는 고도만 시도하고
  다음 차례에 다시 냅니다.

## 0-10. 보류된 공지·둘째 공지·관제 권고·비행 보고서 (2026-09-10)

0-9 의 "모델이 읽은 것은 사람 없이 절대 안 걸림" 을 화면과 원장까지 끌고 왔고, 거절이 쌓인 기체에 런타임이
**권고**(advisory)를 씁니다. 권고는 정보입니다 — 실행·잠금·판정 어느 것도 바꾸지 않고 다음 신청은 똑같이
판정됩니다(검증 스크립트가 권고 전후의 의도·잠금·공역 판본·보류 카드·지출이 같음을 확인).

### 보류된 공지 (held)

- 모델이 읽은 공지는 컴파일되는 순간부터 `/state.notices` 에 `held: true, applied: false` 로 있고 `first_breach`
  에도 정책에도 들어가지 않습니다. 승인 화면의 `publish_notice` 카드에 `POST /approve` 하면 `source: "human"` 으로
  걸리고(날던 경로 회수, 새 경로 거절), `/deny` 면 아무것도 걸리지 않고 그 id 는 다시 묻지 않습니다.
- 사람이 답하기 전에 창이 닫히거나 공지가 피드에서 빠지면 카드·배너가 사라지고 열린 원장 항목이 outcome `lapsed`,
  code `notice_lapsed` 로 닫힙니다. 창이 닫힌 뒤의 승인도 `notice_lapsed`(걸리지 않음). 창이 열리기 전에 승인하면
  `held: false, applied: false, source: human` — 배너는 "confirmed by a person; applies when the window opens"
  (KR 키 `b_notam_confirmed`), 칠하지 않음. 창이 닫히도록 적용 못 한 확인분은 잊습니다(`stale_confirmed`).
- **모델 읽기는 세계 스레드 밖**(`Runtime.notice_async`, 데몬 스레드, 예산 `NOTICE_TIMEOUT_S` 기본 60 s; id 마다 한 번에
  하나, 판이 바뀐 뒤 온 답은 버림). 전에는 세계 폴링 스레드 안에서 동기로 불러 읽는 동안 런타임 틱이 멈췄습니다
  (라이브 2 수정 전: 판마다 틱 1351 에서 20 s 얼어붙고 sim 과 90틱 차이, 4판 중 3판은 20 s 타임아웃으로 못 읽음).
  수정 후: 8.8 s(두 번째 실행) / 21.0 s(첫 실행 — 예전 예산이면 잘렸을 것)에 읽어 보류, 틱 정지 0 s, sim 과 최대 5틱.
- 지도: 적용된 런타임 공지 폴리곤은 바닥에 칠하고(sim 구역 id 와 중복 제거) 보류된 것은 칠하지 않습니다.
  `map.html?rt=<port>&sim=<port>` 로 다른 스택을 봅니다(`window.__attache.api`). 기록에 `notice_*` 코드 문구.
- 승인 화면(`ui/index.html`)이 모델이 지은 공지 이름을 이스케이프 없이 innerHTML 에 넣던 것을 고쳤고(`safe()`),
  `notam.from_model_form` 이 이름에서 `<>&"'` 를 뗍니다 — 모델 출력이 사람 게이트 화면에서 실행되지 않게.
- `_apply_notices` 가 HTTP 스레드(승인)와 세계 스레드에서 동시에 돌아 같은 공지를 두 번 걸고 같은 기체를 두 번
  회수할 수 있던 것 → 잠금.

### 둘째 공지 — 문법이 못 읽는 문장

- sim `MEDEVAC_TEXT`: "MEDEVAC INBOUND HARLEM HOSPITAL HELIPAD. KEEP CLEAR WITHIN 0.5 NM OF 404852N0735623W …",
  0918–0928Z = **틱 1350–2100**(구역 공지 525–900 다음), 반지름 0.5 NM(1 NM 은 10.8 km² 로 `MAX_AREA_M2` 4 km² 를
  넘어 모델 답이 보류가 아니라 폐기됨), 중심 할렘 병원(40.8144, −73.9397). `parse_notice` 가 None 을 내는지 sim 이
  assert 합니다. Super 모델이 없으면 `notice_unreadable` 한 번 + 배너에 원문("not yet read by the runtime").
- 로컬 30B 대역은 양식은 맞추지만 **폴리곤을 잘못 놓습니다**(녹음 4건 전부 ≤200 m 조각, 3 km 남쪽). 그래서 fixture
  `notices_super.json` 에 녹음(label `ollama-recorded`)과 손으로 쓴 기대 답(label `reference`, 16꼭짓점 0.5 NM 원,
  `via: authored` 로 표시) 둘을 두고, 승인→회수 경로는 reference 와 단위 시험으로만 검증했습니다. 사람 게이트가
  장식이 아닌 이유입니다. sim 은 MEDEVAC 원을 점수판에 넣지 않습니다(안 읽혀도 점수는 안 변함).
- 씨앗 7 하네스(4000틱): 모델 없음 / reference 보류만 / reference 자동 승인 세 경우 모두 런타임 세계 위반 0, 배달 22.
  자동 승인 때는 세인트니컬러스 착륙장(중심에서 792 m, 원 안)이 막혀 교차 거절 13 → 60.

### 관제 권고 (TOWER ADVISORY)

- 언제: 기체 하나가 **같은 막힘**(action·code·policy_hit·blocked_kind·volume·asset·until_tick 서명)으로 **3번째**
  거절될 때 한 번(`ADVISORY_AFTER`), 그 막힘에는 다시 없음; 또는 거절 뒤 `decline_job` 이 **실행**될 때
  (`decline_after_refusals`, 중복 거절된 decline 은 제외). 성공·decline·판 교체에 초기화. 처음 "연속 3번" 으로 두니
  착륙 예약을 몇 틱마다 다시 내는 기체가 65분에 190건(거의 전부 hold)을 쌓아 서명 기준으로 바꿨습니다 —
  씨앗 7 하네스 14 → 2, 수정 후 라이브 첫 6분 1건, 다음 10분 0건.
- 선택지는 코드가 만들고 같은 판정에 넣습니다: `hold`(교차·착륙 거절에 `blocked_until_tick` 이 있고 땅에 있을 때만)
  → `climb`(마지막 legs +30 m = `CLIMB_M`; 에이전트 `ALTITUDE_SHIFT_M` 과 같은 값이지만 런타임은 에이전트 패키지를
  import 하지 않아 상수 따로) → `notice_window`(막은 것이 창 있는 공지) → `decline` → `escalate`. 규칙 선택 = 이 순서로
  첫 합법. Super 는 **목록 안의 합법 id 하나 + 400자 요약**만 낼 수 있고 밖의 답은 `llm.discard` + 규칙 선택 + 템플릿.
  괄호·따옴표·대소문자는 벗기고 봅니다 — 수정 전 라이브에서 30B 가 브리프의 `- [hold]` 를 그대로 `"[hold]"` 로
  돌려줘 160건 중 155건이 규칙 폴백이었습니다. 모델 호출은 데몬 스레드(`ADVISORY_TIMEOUT_S` 12 s), 거절 응답은 기다리지
  않고, 판이 바뀐 뒤 온 답은 버립니다. `reserve_pad` 를 `resource` 만으로 낸 경우도 목적지 검사에 넣습니다.
- 원장: action `advisory`, verdict auto, outcome `noted`, `detail {resource, chosen, trigger, source}`,
  `context.checks_run ["advisory"]`, params = 권고 전체. `/state.advisories` 는 기체별 최신
  `{asset, tick, at, ledger_id, trigger, refusals[], options[{id, label, legal, why, until_tick?, shift_m?}], chosen, summary, model, source}`.
- 화면: 아래 가운데 TOWER ADVISORY 카드(12 s, 클릭하면 그 기체로; 불가 선택지는 취소선, 고른 것 표시; 규칙 요약은 키로
  조립, super 요약은 textContent 로 원문), 기록에 `advisory · <선택>` 줄. `source: super` 카드는 fake Super 검증 스택과
  수정 후 원장에서 확인, 라이브 화면 캡처는 없음.

### 사람 몫 카드 (HUMAN verdict)

- 전에는 `human` 판정이 원장에 한 줄도 없었고(65분 스택: drone-04 가 기체 한도 $320 을 넘은 뒤 309번 `human`, 원장
  0줄) 카드가 판이 바뀌어도 남았습니다(5장). 이제 모든 `human` 판정이 원장 항목을 엽니다(pending → 승인/거절/lapsed,
  같은 id). 중복 재신청은 같은 결정을 돌려주며 `outcome: waiting`, `detail.waiting_on`. 판이 바뀌면 보류 공지는
  `notice_lapsed`, 나머지 카드는 `card_lapsed` 로 닫힙니다. 지도는 `human` 줄을 승인 회랑으로 그리지 않고 HUMAN 으로.
- 한도 자체(drone-04: 90초에 착륙 예약 $28 × 8 + fast_charge $60 × 2 등 $490)는 그대로 — 0-5·0-7 의 예산 질문.

### 비행별 보고서 `GET /ledger/report`

- `?asset=<id>&format=json|md`. 원장 **파일 전체**(`Ledger.read_all`, 메모리 200줄 아님)를 신청 id 로 접어 비행 하나 =
  `{proposal, intent, action, filed_at, filed_tick, author, drafter, draft_attempts, checks_run, refusals[], duplicates,
  approved{tick, code, resolution, holding_for, altitude_shift_m, approved_by, outcome, ledger_id}|null, failed,
  conformance[], recalled|null, withdrawn|null}` + 기체별 `advisories[]`. md 는 `text/markdown`(`|`·줄바꿈 이스케이프).
  중복 거절은 `refusals` 가 아니라 `duplicates` 에, 실행 실패는 `failed` 에. 재신청은 같은 id 를 덮어씁니다.
- 코드는 `attache/runtime/reports/`(하위 패키지) — `tests/test_drafter RuntimeNeverReadsTheDrafterTest` 가
  `attache/runtime/*.py` 에서 `drafter` 글자를 grep 하므로 읽기 전용 뷰는 한 층 아래에 둡니다. 16.5 MB 원장에 JSON
  1.28 s, 쓰기 잠금 최대 약 35 ms.

### 시험·캡처

`PYTHONPATH=. python3 -m unittest discover -s tests` → 276개(skip 4, 라이브 스택 떠 있을 때 약 8~15분),
`node --test tests/test_map.mjs` → 34개. 새 파일 `test_agent_draft_timing.py`(초안 타이밍·데몬·재고정),
`test_runtime_advisory.py`, `test_runtime_report.py`, `test_human_cards.py`, `test_propose_form.py`; `test_notam.py` 에
둘째 공지·보류·lapse. 캡처(세션 스크래치, 소실): 보류 배너 `fix-live-notam.png`, TOWER ADVISORY 카드 `live-advisory.png`,
nano 승인 회랑 `live-approved-nano-corridor.png`, 원문 배너 `live-notam-medevac-raw.png`. 지도 콘솔의 "Expected value
to be of type number, but found null" 경고 3건은 OpenFreeMap positron 스타일 것(빈 페이지에서도 3건), favicon 404 는
`data:,` 로 막음.

## 0-11. 화면·판정 손질 (2026-09-10 새벽 — 사용자가 화면을 보고 짚은 것)

| 증상 | 원인 | 고친 곳 |
|---|---|---|
| 빨간 거절 뒤 노란 재작성 없이 초록 회랑이 불쑥 | 재생 큐가 "아직 시작 안 한 단계"(앞 단계 뒤로 예약)를 "끝난 단계"와 같이 지움. 승인이 빨강 끝나기 직전(5.6~5.9 s)에 도착하면 예약됐다가 다음 프레임에 삭제 | `expireDenials` 는 시작 전 단계를 남김. 큐는 실시간보다 한 단계까지만 뒤처짐(밀린 거절 재생은 버림). 노드 테스트 2개 |
| 회랑이 낮은 건물을 파고듦 | 판정 자료가 40 m 이상 건물뿐이고 최저 순항 40 m → 33~37 m 건물 위를 40 m 로 승인(라이브 79구간 중 5건, 전부 이 경우) | 자료를 **20 m 이상**(34,581동, 17 MB; 40 m 자료의 id 는 발자국이 같으면 그대로 유지)으로 다시 뽑고 최저 순항 **70 m**(자료 문턱 20 + 이격 50). 단 70 m 는 **천장이 허락하는 곳에서만** — 천장 61 m 인 FAA 칸 26개가 맨해튼을 가로질러 깔려 있어 70 m 를 못 박으면 센트럴파크 북쪽·할렘 착륙장이 전부 못 가는 곳이 됨. 낮은 칸에서는 천장−1 m 로 날고, 그 칸의 40 m 미만 건물은 이격 20 m(`geo.building_clearance_m`, 742동), 40 m 이상은 50 m 그대로(= 옆으로 돌아야 함). 시뮬레이터가 건물을 넣을 때 칸 천장을 보고 이격을 붙여 런타임에 넘김 |
| 착륙장 30곳 중 12곳이 "둘레 50 m 안에 건물" 로 착륙 불가 | 20 m 자료에 20~26 m 건물이 둘레에 걸림 | 옥상 40 m 미만 건물은 착륙 둘레 15 m(하강 기둥 폭 = 항법 오차 10 + 도착 반경 6), 높은 건물·구역은 50 m. 그래도 안 되는 5곳은 좌표를 10~50 m 옮김 — 센트럴파크 이스트메도는 5번가 동쪽(공원 밖 주택가)에 찍혀 있어 공원 안(40.7887, −73.9600)으로 |
| 재시도 피드백에서 돌아야 하는 건물이 빠짐 | 구간의 첫 장애물만 보고 지나친 자리부터 다시 묻다가 짧은 구간 끝의 낮은 건물 뒤에서 멈춤 | `geo.leg_breaches` — 한 구간이 어기는 것 전부를 진행 순서대로. 판정 기준은 `first_breach` 와 같고 판정 자체는 그대로 첫 번째만 |
| `sim.world` 불러오기가 멈춤 | 건물을 넣으면서 동마다 천장을 물어 색인이 3만 4천 번 다시 만들어짐 | 이격을 넣기 전에 한꺼번에 계산(1.6 s) |
| 하네스에서 drone-03 이 4000틱 안에 창고로 못 돌아옴 | 둘째 정차가 drone-02 와 같은 착륙장이라 "한 착륙장에 두 대는 없습니다" 로 1300틱(212번) 거절되며 첫 정차에 앉아 있었음. 고친 뒤에도 모닝사이드 왕복이 4,040틱(거절 forbidden 5·traffic 7) | 배차가 다른 기체가 가고 있는 착륙장을 피함(`_assign_job`). 한 판을 **5,000틱**으로(`ROUND_TICKS` 기본값, 하네스 TICKS). 계획기는 같은 공역 판본 안에서 같은 자리→착륙장 답을 기억(`Router._route_memo`; 할렘 쪽 첫 계획 60~160 s) |
| 세로 점선이 정육면체 | 8 m 정사각 토막 | 회랑 점선을 세운 모양: 폭 18 m·두께 3 m 판, 36 m 토막·14 m 간격, 폭은 진행 방향에 직각 |
| 창고가 점(원) | 표지 하나 | 타일 건물 바닥면(`footprints` 보이지 않는 fill 레이어로 조회 — 기울인 3D 를 점으로 물으면 옆면이 잡힘) 중 표지 60 m 안 가장 큰 동을 창고색으로. 타일 건물 하나가 단지 전체(다중 다각형 113~231개)라 동 단위로 고름 |
| 창고 근처 아무 데나 앉았다 뜨는 듯 | 자리 넷이 마당에 있었고 화면에 안 그려짐 | 자리를 창고 **옥상** 긴 축(97 m) 위 31 m 간격 한 줄로(`SEATS`, 기체별 고정 `BAY n · drone-0n`, 바깥 둘은 가장자리에 걸침). 시뮬 고도는 지면 기준이라 자리 12 m 안·옥상보다 낮으면 옥상 위 0.8 m 로 올려 그림(`roofLifted`). 22 m 로 두었더니 옆 자리에 내리는 것이 "서 있는 기체 30 m 안" 으로 거절되고 점수판에 분리 상실이 찍혔음 — 31 m 는 그 규칙 바로 밖. 이륙 기둥(회랑 폭 40 m)은 여전히 겹쳐 동시에 뜨면 런타임이 한 대를 기다리게 함. 비상 착륙대(`pad:launch`)는 옥상이 아니라 마당 동쪽으로 |
| 카메라 버튼 | 사용자는 키 조합을 글로 원함 | 왼쪽 세로 중앙 `#keys` 판: Shift+←→ 회전, Shift+↑↓ 기울이기, + − 확대, N 정북, H 처음, 1~4 기체로, 우클릭 드래그. 지도 초점 없이도 먹게 문서 keydown 에서 처리하고 MapLibre 기본 키 처리는 끔(두 번 돌던 것) |
| 피드의 "model" 태그 | 신청서 작성자 티어 단어 | "Agent" (EN/KR) |
| 4B 가 마당에 선 기체에 `charge` 를 쓰고, 옥상 자리의 기체가 `reserve_pad`(착륙대 예약)를 일곱 번 써 옆 자리 기체 위로 내리려다 전부 거절됨. 착륙 예약 $28 × 8 로 기체 한도 $320 초과 → 사람 카드 | 충전 순환은 뺐는데 양식 목록·규칙에 남아 있었고, 모델이 걱정거리와 무관한 행동을 고를 수 있었고, 돈 한도가 아직 판정에 있었음 | 모델이 고를 수 있는 것은 걱정거리에 맞는 것뿐(`propose.allowed_for`: 배달·이륙·포기, 고장 때만 비상 착륙·자율주행 해제). charge/fast_charge/divert_ground 는 목록에서 제거, `needs_charge` 감지 제거. `fleet.yaml` 한도 `null` = 런타임은 돈을 판정하지 않음(숫자를 넣으면 다시 켜짐, 예산은 운영사 몫). 시뮬 기단 한도도 None 허용 |

- 20 m 자료로 바꾼 뒤 A* 시간: 창고→워싱턴스퀘어 0.1 s, →세인트니콜라스 4.7 s, →토머스제퍼슨 15 s, →할렘미어 159 s(첫 계산; 낮은 칸을 도는 탐색이 큼). `test_cycle` 12개 374 s. 에이전트는 계획을 초안 스레드처럼 밖에서 돌리지 않으므로 긴 계획은 그 기체가 자리에서 기다리는 시간이 됨 — 0-7.
- Docker: `docker/Dockerfile` 시뮬 이미지에 `configs/airspace` 가 없어 시작 즉시 죽던 것을 넣고, 화면 이미지는 `scripts/serve_ui.py`(no-store). `compose.yaml` 은 드론 서비스마다 `LLM_URL_1..4`(로컬 함대는 `host.docker.internal:1143x`), Super/Ultra 는 런타임에만.

## 0-7. 남은 것

1. 강 건너(뉴저지) 착륙장 — OSM 건물 보강 후. 지금 뉴저지로는 안 갑니다.
2. 예산 코드 제거 여부(0-5). 라이브에서 drone-04 가 판 중간에 기체 한도 $320 을 넘어(착륙 예약 $28 씩 재신청)
   이후 신청이 전부 `human` 카드로 갔습니다 — 카드는 이제 원장에 남고 판마다 닫히지만(0-10), 한도를 런타임에 둘지는
   미결. 실행 못 할 행동(패드 밖 fast_charge)에 지출을 잡는 것도 같이.
3. 구역 폐쇄 시나리오 유지 여부(0-5). 둘째 공지(MEDEVAC, 사람 확인)를 더했으니 함께 판정.
4. Nebius Token Factory — `NEBIUS_API_KEY` 가 없어 이 Mac 에서 한 번도 안 불렀습니다. Super 120B 의 공지 읽기·권고
   고르기와 Ultra 중재는 로컬 대역(30B)으로만 봤습니다. 데모 배선은 Nebius.
5. 4B 는 짧은 경로만: 맨해튼 10 km 횡단은 전부 A* 폴백, 거절 사유를 준 재질문은 65분에 승인 0. 천장 칸 안 건물의
   GO AROUND 수정이 긴 횡단에 효과가 있는지 미측정. 더 가려면 json_schema response_format.
6. 눈으로 못 본 것: 거절 순간에 초안이 시작되는 타이밍의 화면, `source: super` 권고 카드의 라이브 화면(원장에는 있음),
   KR 문구, 좁은 화면. 로컬 30B 는 0.5 NM 원을 한 번도 제대로 못 그렸습니다(승인→회수는 authored fixture 로 검증).
7. `awaiting_human` 카드는 지도에 안 그립니다(승인 화면 `index.html` 에만). `duplicate` 거절(직전과 같은 신청)이 원장에
   많이 남습니다(65분에 70건) — 예전부터, 손 안 댐. 판 중간에 런타임을 재시작하면 의도 등록부가 사라집니다(재시작
   직후 분리 상실 1 관측).
8. ruff 는 예전부터 통과하지 못하던 상태 — 지금 28건(전부 이전 파일 줄), 이 세션에서 더한 것 없음.

---

---

## A. 시뮬레이션이 실제 같아야 한다

| # | 요구사항 | 상태 | 어디에 |
|---|---|---|---|
| A1 | 드론이 너무 빠르다. 실제 속도로 | **됨** | `sim/world.py` `CRUISE_MPS=22`. 격자가 아니라 미터로 이동 |
| A2 | 건물 좌표·고도를 실제 데이터로 | **됨** | `scripts/fetch_buildings.py`, 25m 초과 3,284동 |
| A3 | 건물 사이로 날아다니는 모습 | **됨** | 순항 25m. 25m 넘는 건물이 전부 장애물이라 길이 블록 사이로 돌아감 |
| A4 | 이륙 후 이동, 도착점 위에서 수직 하강 | **됨** | `_at_cruise`, `_hold_altitude` |
| A5 | 승인 즉시 출발 금지. 확인하고 출발 | **됨** | `CLEARANCE_TICKS=34` |
| A6 | 하역·적재·착륙 상태 표시 | **됨** | `dropping` / `loading` / `landing`, 지도 기체 옆에 표시 |
| A7 | **시나리오 시작 시 전 기체가 창고에서 적재** | **됨** | 마당 제 자리(한 줄)에서 `loading` 으로 시작 |
| A8 | **적재량을 대가리 위 상자 스택으로** | **됨** | `Vehicle.load`, 네모 상자(`box()`), 1초에 하나씩 |
| A9 | 순항 고도를 낮춰 더 비집을지 | **됨(25m)** | 사용자 "건물 우회해서 가게" → 25m |
| A10 | 배터리는 런타임이 관리할 게 아님 | **됨** | 운영사 에이전트가 감지·신청, 런타임은 판정만. 항속 35분, 추락 0 |
| A11 | 배터리 발화 리콜 빼라 | **됨** | 감항성 지시(기종 운항 정지)로 교체 |
| A12 | 이륙장 하나만, 이름 변경 | **됨** | `pad:launch`, 창고 옆 |
| A13 | 기체 수 (네 대) | **됨** | 세계당 4대, 기종 반반 |

## B. 판정이 화면에 보여야 한다

| # | 요구사항 | 상태 | 어디에 |
|---|---|---|---|
| B1 | 경로를 점점점 앞으로 그려나가기 | **됨** | `stageWindow`, `GROW_MS=3600` |
| B2 | 산출 중은 다른 색 · 가는 노란 점선 | **됨** | `pending-line` |
| B3 | 승인 전 초록 절대 금지 | **됨** | 판정 전에는 `pending`, `growing` 으로 회랑도 감춤 |
| B4 | 승인 단계 표현 (바로 출발 금지) | **됨** | `AWAITING VERDICT…` → `APPROVED` 3회 깜빡 → 출발 |
| B5 | 거절도 보여야 함 | **됨** | `rejected-line`, 붉은 점선 |
| B6 | 거절 사유가 나와야 함 | **됨** | 지도 라벨 + 알림 카드 + 원장, 전부 구조화된 값에서 조립 |
| B7 | 막은 건물·구역을 그 자리에 | **됨** | `blocker` 붉은 입체 + `breach` 지점 + 높이 라벨 |
| B8 | 거절되면 다시 노란색으로 재산출 | **됨** | 두 번째 신청도 `drawing` 단계를 거침 |
| B9 | 확 회수하지 말고 서서히 | **됨** | 되감기 대신 제자리에서 흐려짐 |
| B10 | 지나온 경로는 지우기 | **됨** | `sliceCurve` 로 남은 구간만 |
| B11 | 글자가 왔다갔다 하지 않게 | **됨** | 라벨을 출발점에 고정 |
| B12 | 애니메이션이 너무 빠름 | **됨** | 3.6초 그리기 + 1초 판정 대기 |

## C. 화면과 조작

| # | 요구사항 | 상태 | 어디에 |
|---|---|---|---|
| C1 | 전부 영어로, 눈에 띄게 | **됨** | 기본 EN, KR/EN 토글 |
| C2 | 밑에 설명(범례) 깔끔하게 | **됨** | 9줄 → 6줄 |
| C3 | 배터리 % 지도에서 제거 | **됨** | 클릭 상세에는 남음 |
| C4 | 기체 클릭하면 상세 | **됨** | 팝업 |
| C5 | 회전·기울이기 단축키 | **됨** | 나침반 컨트롤 + 안내문 |
| C6 | 창고가 왜 빨간가 | **됨** | 구역이 도형 두 벌이었고 만료도 안 됐음. 둘 다 고침 |
| C7 | 대가리 위 굵은 선 제거 | **됨** | 고도 기둥 제거 → 그래도 남던 것은 `flightpath-curtain` 이었음. 제거 |
| C8 | Direct 가 Runtime 을 따라다녀 지저분 | **됨** | 지도 기본은 RUNTIME 만. 토글로 BOTH/DIRECT |
| C9 | 페이지 열면 시나리오 처음부터 | **부분** | RESTART 버튼은 있음. 자동 리셋은 아님 |
| C10 | 왜 정적 페이지여야 하나 | **답변** | 화면은 보기만 하는 쪽이라 서버가 필요 없었음. RESTART 때문에 이제 POST 하나 씀 |

## D. 사용자가 물은 것 (답변 완료, 코드 없음)

- **판정은 Nvidia 모델이 하나?** → 아니오. 전부 결정적 코드. 모델은 신청서 작성과 중재
  후보 선택만 하고, 못 하면 규칙이 대신함.
- **뉴욕은 건물 사면 하늘도 사는 것 아닌가?** → 그건 조닝의 개발권(FAR). 비행은 연방 관할.
  진짜 벽은 NYC §10-126 이착륙 금지.
- **로어맨해튼에 비행금지가 없는 게 확실한가?** → FAA 서비스 직접 조회함. 그 구역 4칸,
  전부 400ft. 0ft 없음. 국가안보 제한구역·금지구역·스타디움도 뉴욕 상공 0건.
- **건물마다 비행금지 표시가 있지 않나?** → 없음. 건물은 물리적 장애물로만 작동.
- **직접 조종은 런타임이 적용되나?** → 아니오. 별도 패키지·이미지·네트워크. 감사할 기록
  자체가 없음(`unrecorded_actions == actions`).
- **직접 조종을 빼버릴까?** → 권고: 계산은 유지(대조군이 없으면 주장이 증명 불가),
  지도에서만 기본으로 감춤. 이미 그렇게 해둠.


---

# E. Codex 인계 — 남은 일

시작 상태: 테스트 68개(python) + 19개(node) 통과. `PYTHONPATH=. python3 -m unittest discover -s tests`,
`node --test tests/test_map.mjs`. 두 세계 점수판은 `PYTHONPATH=. python3 tests/test_two_worlds.py`.
**끝낼 때마다 셋 다 돌리고, 점수판의 런타임 열이 전부 0인지 볼 것.**

## E1 · 적재 순환 (가장 큼, 나머지가 여기 딸림)

지금 기체는 창고로 돌아오지 않습니다. 배달을 마치면 그 자리에서 다음 주문을 받습니다.

- `sim/world.py`
  - `Vehicle.load: int` 추가. `PARCELS_PER_TRIP = 3`.
  - `fresh_fleet()`: 이륙장 좌표에 `state="loading"`, `alt=0`, `work_ticks=LOAD_TICKS` 로 시작.
    **시나리오 첫 장면이 "네 대가 창고에서 싣는 중" 이어야 합니다.**
  - `act()` 의 `depart`: `LOAD_TICKS` 뒤 `load = PARCELS_PER_TRIP`.
  - `_deliver()`: `load -= 1`. 남으면 다음 주문, 0이면 `job_x=None` 으로 두고 복귀 대상.
- `attache/agent/detect.py`: `load == 0` 이고 이륙장 밖이면 `needs_reload` 를 냄.
  `propose.by_rule` 이 그걸 `reserve_pad` 로 받게. **두 세계가 같은 detect 를 쓰므로 여기 한 곳만.**
- 검증: 한 판에서 각 기체가 창고 → 배달 3 → 창고를 최소 한 바퀴.

## E2 · 화면에서 단계가 구분돼야 함

상태 문구(`LOADING… / LANDING… / UNLOADING…`)와 기체 옆 표시는 이미 있습니다
(`WORKING` 집합, 아이콘의 `work` 속성). E1 이 끝나면 순환이 생겨 실제로 뜹니다.

- **짐 상자**: `cargoStack()` 이 기체 위로 정육각을 개수만큼 쌓게 이미 만들어 뒀습니다.
  `Vehicle.load` 만 생기면 살아납니다. `cargo-box` 레이어.
- **적재/하역 구분**: 지금은 둘 다 노란 상자입니다. 싣는 중이면 상자가 하나씩 늘고,
  내리는 중이면 하나씩 줄게 하면 눈으로 구분됩니다(`work_ticks` 비율로 개수 보간).

## E3 · 맨해튼 붉은 띠 우회 (핵심 시나리오, 성립 확인함)

**맨해튼 한가운데에 KLGA 0ft 격자가 붉은 띠로 지나갑니다.** 브루클린에서 그 너머로
직선을 그으면 거기 걸리고, 우회로가 나옵니다. 사용자가 원하는 그림이 바로 이것입니다.

측정해서 확인한 것 (출발: 이륙장 40.7019/-73.97049, 순항 55m):

| 목적지 | 직선 | 우회로 |
|---|---|---|
| Upper West Side 40.787/-73.975 | **KLGA 0ft 격자에서 거절** (40.750/-73.973) | **7구간, 통과** |
| West New York NJ 40.788/-74.010 | 건물 56m 에서 거절 | 6구간, 통과 |
| Cliffside Park NJ 40.821/-73.988 | 건물 65m 에서 거절 | 없음 → `decline_job` |
| Midtown 40.759/-73.985 | 건물 56m 에서 거절 | 없음 → `decline_job` |

**할 일.**
- `sim/world.py SERVICE_RADIUS_M` 을 4km → 10~11km 로. 주소는 이미 13km 까지 있고
  (총 832개, 9~11km 구간에만 201개) 재수집 없이 바로 됩니다.
- 공역도 이미 lat 40.683~40.833 을 덮습니다. **재수집 불필요.**
  건물만 뉴저지가 비어 있는데, 목적지를 맨해튼 북쪽으로 잡으면 그것도 불필요합니다.
- **왕복이 길어집니다.** 10km 편도 = 570틱. `ROUND_TICKS` 를 1800 → 4000 이상으로.
- 검증: 한 판에서 `airspace` 거절 5건 이상, 그중 0ft 격자 사유가 절반 이상.
  화면에서 노란 직선 → 붉게 3번 깜빡 → 노란 우회로 → 초록 승인 → 비행.

## E3b · 강 건너까지 (선택)

**브루클린 출발 → 직선이 붉은 비행금지 구역을 관통 → 런타임 거절 → 우회 → Cliffside Park
/ West New York 도착.** 이 장면이 자주 나와야 합니다.

지금 안 나오는 이유: 배달 반경 4km 라 목적지가 전부 로어맨해튼이고, 거기엔 FAA 0ft 격자가
없습니다(직접 조회 확인). 0ft 는 퀸스·LGA·TEB 쪽 — **강을 건너야 걸립니다.**

- `sim/world.py` `SERVICE_RADIUS_M` 확대(예: 12km) 또는 강 건너 주소를 따로 섞기.
  Cliffside Park 약 40.821/-73.988, West New York 약 40.788/-74.010.
- 공역 재수집: `python3 scripts/fetch_airspace.py --bbox=-74.06,40.62,-73.85,40.84 --out configs/airspace/nyc.json`
  (그 범위에 358칸, 0ft 95칸 확인해 둠)
- 건물 재수집: `python3 scripts/fetch_buildings.py --bbox=... --min-height-m 25 --out configs/airspace/nyc_buildings.json`
  (뉴욕시 데이터라 뉴저지는 안 나옵니다. 필요하면 OSM 으로 보강)
- **반경을 늘리면 왕복이 길어집니다. `ROUND_TICKS` 도 같이 올릴 것.**
- 검증: 한 판에서 `airspace` 거절 5건 이상, 절반 이상이 0ft 격자 사유.

## E4 · 기체 네 대 (확정)

**런타임 기단 4대.** 지금 2대입니다. 이륙장이 하나라 경쟁이 늘어 잠금표와 중재가
더 자주 걸립니다 — 그게 런타임이 있는 이유 중 하나라 오히려 좋습니다.
`sim/world.py fresh_fleet()`, `scripts/dev.sh`, `compose.yaml` 세 곳을 같이 고칠 것.
기종은 절반씩 섞어라(`dv-x500` 2대, `dv-hexa` 2대) — 감항성 지시가 한 기종에만 걸리므로
"같은 기단인데 절반만 멈춘다"가 보입니다.

## E4b · 목적지는 자유 좌표

목적지를 고정하지 마라. 서비스 반경 안의 실제 주소에서 무작위로 뽑고
(`pickable_addresses()`), 장애물은 라우터가 알아서 피한다.
직선이 막히면 거절 → 우회가 나오는 것이 이 데모의 전부이므로,
**어떤 좌표가 걸릴지 미리 정해두면 안 된다.** 우연히 걸려야 진짜다.

## E5 · 순항 고도 결정 (사용자 판단 필요)

같은 12쌍 기준, 50m 격자: 70m·55m·40m 는 평균 4구간, **25m 는 9구간**.
25m 라야 건물 사이를 실제로 비집습니다. 다만 실제 배달 드론은 45~60m(Wing 약 45m).
바꾸는 곳은 `attache/core/route.py` `CRUISE_ALT_M` 한 줄.

## E5b · 경로를 모델이 그리게 할 것인가 (선택, 논증이 세짐)

지금 경로는 `attache/agent/planner.py` 의 A* 가 그립니다. 결정적 코드이고 모델이 아닙니다.
런타임은 **누가 그렸든 같은 기준으로 판정만** 합니다.

여기가 이 프로젝트의 가장 강한 논증이 될 수 있는 자리입니다:
**Nemotron 이 경로를 그려도 런타임의 보장은 하나도 안 바뀝니다.** 모델이 그린 경로도
`first_breach` 를 통과해야 하고, 못 통과하면 거절입니다.

- 넣는다면: `OperatorPlanner.draw()` 옆에 모델 경로 생성기를 두고, 양식(경유점 목록)이
  아니면 버리고 A* 로 되돌린다 — `propose.py` 가 신청서에 쓰는 방식과 같다.
- 화면에는 "이 경로를 누가 그렸는가"(A* / 모델)를 표시하고, **판정 결과는 그것과 무관하다**는
  것을 보여준다.
- `NEBIUS_API_KEY` 가 필요하다. 이 세션에서 모델은 한 번도 호출된 적이 없다.
- **사용자에게 물어보고 진행할 것.**

## E6 · 미착수

- **NYC §10-126** 이착륙은 지정 장소에서만. `Volume` 이 아니라 `Policy`/자원 규칙.
  뉴욕에서 드론 배달을 실제로 막는 진짜 규정.
- **도로 회랑** 벡터 타일에 도로 9,792개. Part 107 완화책 근거가 생김.
- **Docker / Nemotron / Tavily** 이 세션에서 한 번도 못 돌림.

---

# E0. 화면 규칙 — 먼저 할 것 (사용자가 마지막에 못 박은 것)

## E0-1 · 지도에 붙은 선은 전부 없앤다 — **됨** (2026-09-09, 화면 확인)

`pending/approved/rejected` 지면 소스·레이어를 지웠습니다. 경로는 `flightpath` 소스 한 벌이고,
기체마다 `flightpath:<asset>` 레이어를 만들어 따로 깜빡이고 흐려집니다(extrusion 불투명도는
레이어 단위라 그렇습니다). 공중 점선은 `curveRibbon()` — 점선 위상이 출발점에 고정돼 지나온
구간을 잘라내도 도형이 밀리지 않습니다. 기본 줌 14.5. 아래는 당시 지시 원문입니다.


경로는 **공중에 뜬 회랑 하나뿐**입니다. 산출중이든 거절이든 승인이든 전부.
지금은 지면 선(`pending-line` / `approved-line` / `rejected-line`)과 회랑(`flightpath`)이
같이 있고, 줌 14 를 경계로 교대하게 해놨습니다. **교대가 아니라 지면 선을 지울 것.**

- `ui/map.html`: 위 세 레이어와 소스 제거. `drawStages()` 가 회랑만 채우게.
- 멀리서는 44m 폭 회랑이 화소 이하가 됩니다. `RIBBON_HALF_M`(현재 11 → 20 이상)을 키우고
  기본 줌을 13.5 → 14.5 로 올려 기본 화면에서 회랑이 보이게 할 것.
- `tests/test_map.mjs` 가 `pending`/`approved`/`rejected` 소스를 봅니다. `flightpath` 의
  `phase` 속성을 보도록 고칠 것.

## E0-2 · 한 기체에 한 번에 한 선만 (고쳐놓음, 검증 필요)

**노란 선 → 거절되어 회수 → 그 다음에 새 노란 선 → 승인.** 절대 겹치지 않습니다.

- `stageRoute()` 가 모든 구간을 기체별 큐(`queued`)에 넣습니다. 앞 구간이
  완전히 사라진 뒤에 다음 구간이 시작합니다.
- **원장은 최신이 앞입니다.** 그대로 큐에 넣으면 승인이 거절보다 먼저 재생되어
  순서가 뒤집힙니다. `renderDenials()` 에서 `at` 오름차순으로 다시 세웁니다.
  (이걸 놓쳐서 순서가 뒤죽박죽이던 버그가 실제로 있었습니다)
- **기체가 여럿이면 각자의 선이 동시에 그려집니다.** 그건 정상입니다 — 순서는
  한 기체 안에서만 지켜지면 됩니다.
- `CLEARANCE_TICKS = 78`(15.6초). 거절 8.8초 + 승인 6.8초를 덮습니다.
- **이 홀드는 지상에서만 걸립니다.** 공중에서 멈추면 그 자리에 붙박이가 되고,
  닫힌 구역 위였다면 거기 머물게 됩니다(실제로 구역 체류 341틱이 나왔습니다).
  그래서 `alt > 1.0` 이면 홀드를 건너뜁니다.
- **남은 어긋남**: 공중에서 새 경로를 승인받으면 기체는 바로 움직이는데 화면은
  아직 재생 중입니다. **E1(적재 순환)이 끝나면 모든 출발이 지상에서 일어나므로
  이 어긋남이 사라집니다.** E1 이 이 문제의 진짜 해법입니다.

## E0-3 · 공역 블록이 경로를 가린다

금지 구역을 **0~200m 벽**으로 세워놨습니다(`cells-roof` 의 `fill-extrusion-height` 200).
회랑은 55m 라 그 벽 **안에 파묻힙니다.** 천장 구역도 판이지만 30/61/91/122m 라
55m 회랑과 겹칩니다. 바닥 채색(`cells-fill`)까지 있어 더 지저분합니다.

**사용자 지시: 구역은 바닥에 색을 칠하는 쪽이 낫다.** 입체로 세우면 경로를 가려버리고,
반투명 덩어리가 여러 겹 겹치면서 깨져 보이기도 합니다.

- 1안(권장): `cells-roof`(입체)를 빼고 `cells-fill`(바닥 채색)만 남긴다. 등급은 색으로
  읽히고, 회랑은 그 위를 지나가므로 절대 안 가려집니다. 높이 정보는 범례와 거절 사유
  (`BUILDING 72 m`, `0–72 m AGL`)에 이미 있습니다.
- 2안: 입체를 남기되 **순항 고도(55m) 아래까지만** 세우고 불투명도를 크게 낮춘다.
  회랑이 그 위를 지나가는 그림이 나옵니다.
- 어느 쪽이든 목표는 하나입니다: **경로가 절대 가려지지 않을 것.**
  겹침 때문에 깨져 보이는 것도 같이 사라져야 합니다.
- fill-extrusion 을 여러 겹 반투명으로 쌓으면 깊이 정렬이 깨집니다. 더 나은 방법이
  있으면 그쪽을 써도 됩니다 — 조건은 위 한 줄뿐입니다.

## E0-4 · 기체는 가다가 멈추지 않는다

**중간에 멈춰 서 있는 기체가 있으면 안 됩니다.** 정지는 목적지에 도착했을 때뿐이고,
그때는 **반드시 작업 중**이어야 합니다 — `LOADING` / `PICKING` / `DELIVERING`(하역).

지금 어기고 있는 곳:
- `CLEARANCE_TICKS`(승인 확인 대기)가 **기체가 있는 자리 그대로** 걸립니다.
  배달을 마치고 공중에서 다음 주문을 받으면 거기서 멈춰 섭니다.
- 승인을 기다리는 동안에도 제자리에 떠 있습니다(`_current_target` 이 None).

**해법은 E1(적재 순환)입니다.** 기체가 배달을 마치면 이륙장으로 돌아가고, 다음 경로
신청과 승인 대기는 **이륙장 위에서 적재하며** 일어납니다. 그러면 멈춰 있는 순간이
전부 작업 중인 순간이 됩니다.
- 그래도 공중에서 멈춰야 하는 경우(구역이 닫혀 회수당함 등)는 상태를 `holding` 이 아니라
  무슨 일인지 쓰인 상태로 두고, 화면에 이유를 붙일 것.
- 검증: 한 판을 돌려 `state` 별 체류 틱을 세고, `cruising`(대기 비행)으로 공중에
  떠 있는 시간이 0 에 가까울 것.

## E0-5 · 상태 텍스트와 짐 표시 디자인

`CHARGING` / `LOADING` / `LANDING` / `UNLOADING` 각각을 **읽히게 디자인**할 것.
지금은 기체 이름 옆에 `·` 로 붙는 평문입니다.

- 상태마다 색을 주고(충전 노랑, 적재 호박, 착륙 파랑, 하역 초록), 진행도를 같이 보일 것
  — `work_ticks` 로 남은 시간을 알 수 있으므로 `LOADING 2/3` 처럼 쓸 수 있습니다.
- **짐 양**: `cargoStack()` 이 기체 위로 정육각을 개수만큼 쌓습니다(`cargo-box` 레이어).
  `Vehicle.load` 만 생기면 살아납니다. 싣는 중에는 하나씩 늘고 내리는 중에는 하나씩 줄어
  **적재와 하역이 눈으로 구분**되게 할 것.
- 기체 위에 두는 것은 **짐 상자뿐**입니다. 선이나 기둥은 절대 다시 넣지 말 것.

---

# F0. 선이 그려지는 순서 (사용자가 못 박은 규칙)

**선은 새로 그리지 않는다. 같은 선의 색이 바뀐다.**

```
경로 산출          노란 점선이 앞으로 뻗어 나감        PLANNING…
판정 대기          그 선 그대로                        AWAITING VERDICT…
  ├ 거절           그 선이 빨갛게 · 세 번 깜빡 · 흐려짐  REJECTED · <막은 것>
  │                그 다음 다시 노란 점선으로 재산출
  └ 승인           그 선이 초록으로 · 세 번 깜빡         APPROVED · <승인자>
                   그대로 진행 경로가 됨. 기체는 깜빡임이 끝나고 출발
```

- 거절선과 승인선의 **모양이 다른 것은 정상**입니다(직선 대 우회로). 같은 신청 안에서
  색만 바뀌어야 한다는 뜻입니다.
- 승인 재생이 도는 동안에는 **실제 비행 회랑을 감춥니다**(`growing`). 안 그러면 승인될 때
  선이 하나 더 생긴 것처럼 보입니다. 이게 사용자가 제일 여러 번 지적한 부분입니다.
- 기체 출발 시점: `sim/world.py CLEARANCE_TICKS`(34틱=6.8초)와 UI 의
  `GROW+CHECK+APPROVED_HOLD`(6.8초)를 맞춰 뒀습니다. 한쪽을 바꾸면 다른 쪽도 바꿀 것.

# F. 방금 고쳐놨지만 눈으로 확인 못 한 것

1. **경로는 한 벌.** 회랑(고도)에 `phase` 를 실어 노랑→초록으로 **같은 덩어리**가 색만
   바뀝니다. 지면 선은 줌 14 위로 완전히 사라집니다. 굵기는 셋 다 5px 로 통일.
2. **기체는 쿼드콥터 입체.** 아이콘 스프라이트를 버리고 몸통 1 + 로터 4 를 고도에 세웠습니다
   (`droneBody()`). MapLibre 5 는 심볼을 고도에 못 띄웁니다(`symbol-z-offset` 부재).
   지면에는 이름표만 남습니다.
3. **회랑의 진행분 삭제.** 지면 선은 잘라냅니다. 회랑은 0.5초 폴링 단위라 그 사이
   진행분이 남습니다 — `flown` 진행값으로 잘라야 합니다.
4. **승인 3회 깜빡임과 출발 시점.** UI 6.8초와 시뮬레이터 대기 34틱을 맞췄지만,
   앞에 거절 재생이 붙으면 어긋납니다.

# G0. 사용자가 말한 시나리오 — 점검표

넘기기 전에 이 목록으로 빠진 게 없는지 확인하십시오.

| 시나리오 | 어디에 | 상태 |
|---|---|---|
| 시작 화면: **네 대**가 창고에서 짐 싣는 중 | E1, E4 | 됨 |
| 목적지 | E4b | 바뀜: 지정 착륙장 12곳 (사용자 지시) |
| 창고 적재 → 이륙 → 배달 → 착륙 → 하역 → 수거 → 창고 복귀 반복 | E1 | 됨 |
| 짐 개수가 기체 위에 상자로 쌓이고, 싣고 내릴 때 늘고 줄어듦 | E0-5, E2 | 됨 |
| `CHARGING/LOADING/PICKING/LANDING/UNLOADING` 문구가 색과 진행도로 | E0-5 | 됨 |
| 기체가 중간에 멈추지 않음. 멈추면 반드시 작업 중 | E0-4 | 됨 (공중 대기 2틱/3200틱) |
| 브루클린 → 직선이 붉은 0ft 띠 관통 → 거절 → 우회 → 승인 | E3 | 됨 (반경 11km, 화면에서 FAA GRID 거절 확인) |
| 강 건너(West New York / Cliffside Park)까지 배달 | E3b | 안 됨 — 뉴저지 건물 데이터 없음 |
| 노란 점선 산출 → 대기 → 같은 선이 빨강(3번 깜빡) 또는 초록(3번 깜빡) | F0 | 됨 |
| 선은 절대 새로 그리지 않음. 한 기체에 한 선만 | E0-2, F0 | 됨 |
| 지도에 붙은 선 없음. 공중 회랑만 | E0-1 | 됨 |
| 구역 표시가 경로를 가리지 않음 | E0-3 | 됨 (바닥 채색, 건물 아래) |
| 승인 깜빡임이 끝나야 기체가 출발 | F0, F4 | 됨 (지상 출발 hold) |
| 건물 사이를 비집고 다님 | E5 | 됨 (25m) |
| 드론이 공중에 떠서 3D 로 보임 | F2 | 됨 (확인) |
| 거절 사유가 화면에 나옴 | B6 | 됨 |
| 막은 건물·구역이 그 자리에 표시됨 | B7 | 됨 |

# I. 고친 버그와 원인 (누적)

| 언제 | 증상 | 원인 | 고친 곳 |
|---|---|---|---|
| 2026-09-09 | 승인된 경로를 날던 기체가 건물 모서리를 스침(139틱 측정, `bldg-1077589`) | `first_breach` 가 8m 고정 간격 표본만 봐서 표본 사이의 짧은 관통을 놓침. 계획기도 같은 함수를 써서 둘 다 통과라고 봄 | `attache/core/geo.py` — 선분을 폴리곤 변과의 교차점에서 나눠 구간마다 판정. 격자 색인으로 후보만 모음. `tests/test_receipt.py` 회귀 시험 2개, 점수판에 `airspace_violations == 0` 단언 추가 |

# G. 절대 어기지 말 것

1. 화면에 있는 것은 전부 뜻이 있어야 한다. 모르겠는 표시는 뺀다.
   (고도 기둥·경로 커튼·궤적선이 이래서 사라졌다)
2. 판정 전에 결과를 보여주지 않는다. 초록은 승인 뒤에만.
3. 런타임이 관리할 것과 아닌 것을 구분한다. 배터리·정비는 운영사, 밖에서 도착해 즉시
   강제돼야 하는 규칙만 런타임.
4. 한 화면에 한 언어(EN/KR 토글).
5. 부산스러우면 뺀다. 선을 더하기 전에 뺄 것을 먼저 찾는다.
6. 직결 쪽을 일부러 못나게 만들지 않는다. 같은 씨앗·같은 감지·같은 신청서 작성기.
7. 판정 로직은 `attache/core/geo.first_breach` 하나뿐이다. 두 번 적으면 반드시 갈라진다.

# H. 순서

E4(한 줄) → E1 → E2 → E3 → E5. F1~F4 는 각 단계에서 화면으로 확인.
