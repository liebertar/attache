# 인수인계 — Attaché

작성 2026-09-09. 지도 작업 전 기준 커밋 `424a0a6`.
Python 테스트 54개 실행, 오류 없음(4개는 pymavlink 없어 건너뜀).
지도 상태·곡선 테스트: `node --test tests/test_map.mjs` 8개 통과.

---

## 1. 이 프로젝트가 뭔가

**드론이 뭔가 하기 전에 반드시 지나야 하는 관문이고, 지나간 것만 기록에 남습니다.**

에이전트는 신청서만 낼 수 있습니다. 실행 코드는 런타임 안에만 있습니다.
경로를 그리는 건 운영사(드론 회사)의 일이고, 그 경로가 규정에 맞는지 판정하고
승인·거절하는 것이 런타임의 일입니다.

대회: Nebius × NVIDIA Global AI Hackathon 2026, Physical AI 트랙.
제출 마감 2026-10-30 10:00 PT. 요건은 `HACKATHON.md` 참조.

## 2. 지금 돌아가는 것

```
docker compose up --build          # 기본. 내장 시뮬레이터 + 승인 화면
make dev                           # Docker 없이 로컬 프로세스로 같은 스택
```

- `http://localhost:3100/map.html`   지도. 뉴욕, FAA 공역, 배달 드론 6대
- `http://localhost:3100/index.html` 승인 화면, 두 세계 점수판, 원장
- `http://localhost:8000/state`      런타임 상태 (잠금, 지출, 정책, 원장)
- `http://localhost:8100/compare`    두 세계 스냅샷

### 데이터는 전부 진짜다
- **공역**: FAA UAS Facility Map 185칸. `scripts/fetch_airspace.py` 가 받아옴. 키 불필요.
  0ft 56칸(허가 없이 비행 불가), 나머지는 15~122m 천장. 격자 없는 곳은 Part 107 기본 400ft.
- **배달지**: OpenStreetMap 맨해튼 실제 주소 832개.
- **물류 기지**: 브루클린 네이비야드 (40.702, -73.970). 충전대 2자리, 기체 3대.

### 두 세계 비교 (1200틱, `PYTHONPATH=. python3 tests/test_two_worlds.py`)

|                    | 런타임 거침 | 직접 |
|--------------------|---------|-----|
| 배달 완료            | 14      | 19  |
| 규정상 불가로 반려      | 78      | 0   |
| 금지 공역 진입         | 0       | 7   |
| 허용 고도 초과         | 3       | 3   |
| 충전대 충돌           | 0       | 29  |
| 기록 없는 실행         | 0       | 37  |

두 세계는 같은 씨앗, 같은 감지 코드(`attache/agent/detect.py`),
같은 신청서 작성기(`attache/agent/propose.py`), 같은 모델을 씁니다.
**다른 것은 배선뿐입니다.** 직결 쪽을 일부러 못나게 만들지 마세요 — 그러면 논증이 무너집니다.

## 3. 구조

```
attache/core/     models(8개 개념) · config · geo(공역 Volume/Airspace) · route(A* 우회) · http
attache/agent/    detect(규칙만) · propose(신청서) · planner(운영사 경로) · loop(가드 에이전트)
attache/llm/      Nemotron 3티어 클라이언트. 양식 안 맞으면 버림
attache/runtime/  policy · authority · locks · arbiter · commit(유일한 실행 경로) · ledger
                  replay(과거를 새 규칙으로 재판정) · service(HTTP)
attache/adapters/ fleet_sim · mavlink_fleet(PX4/ArduPilot) · flockwave(Skybrush)
direct_agent/     "오늘의 배선". 별도 패키지·별도 이미지. 자기 MAVLink/Flockwave 클라이언트 보유
sim/              뉴욕 세계. 조종장치는 검사를 안 함(일부러)
ui/               index.html(승인) · map.html(MapLibre 3D)
configs/          fleet.yaml(한도·금지) · airspace/nyc.json(FAA) · airspace/nyc_addresses.json(OSM)
scripts/          fetch_airspace.py · what_if.py(반사실 재생) · px4_check.py · dev.sh
```

### 지켜야 할 불변식
1. `attache/agent/` 는 `attache.runtime`, `attache.adapters`, `direct_agent`, `pymavlink` 를
   import 하지 않는다. `tests/test_agent_isolation.py` 가 강제.
2. 가드 에이전트 도커 이미지에 실행 코드가 없다. `docker/Dockerfile` 의 `RUN test ! -e` 가 강제.
3. 판정 로직은 `attache/core/geo.first_breach` 하나뿐이다.
   런타임과 계획기가 같은 함수를 쓴다. 두 번 적으면 반드시 갈라진다(실제로 그래서 막혔었음).
4. 원장은 실행 **전에** 열고 실행 **후에** 닫는다. 닫을 때 최종 결정문을 다시 담는다.
5. 모델은 판정하지 않는다. 양식 아니면 버리고, 중재는 통과한 목록의 번호만 받는다.

---

## 4. 사용자가 요청했는데 아직 안 된 것

우선순위 순.

### A. 지도 화면 (1~5 구현 완료, 실제 화면 검증은 남음)
1. **물류 창고 표시.** `worlds.guarded.depot_coords` 에 마커와
   "Delivery Warehouse" 라벨을 붙임. 문서 좌표를 하드코딩하지 않고 응답을 따름.
2. **초기 확대 수준.** `fitBounds` 를 제거하고 중심 `[-73.975, 40.758]`,
   zoom 11.3, pitch 45, bearing -28로 고정. 폴링과 라운드 변경도 사용자 시야를 옮기지 않음.
3. **요청 경로 vs 승인 경로.** guarded 기체의 `route` 를 초록 실선으로 그림.
   기체 마커도 같은 곡선 위에서 진행하고, 통과한 경유점이 응답에서 빠져도 곡선을 유지함.
   최종 거절 원장의 `proposal.params.legs` 는 붉은 점선으로 8초간 표시.
   우회로를 화면에서 만들지 않고 운영사가 재신청하여 실행된 `route` 를 표시함.
4. **거절 시 알림.** 기체·행동·사유를 담은 "모니터링 필요" 경고를 8초간 표시.
   같은 원장 항목은 반복 알리지 않고, pending·오래된 기록은 제외함.
   경로가 없는 거절은 경고만 표시. 폴링 실패 중에도 만료되고 새 라운드에서 초기화됨.
5. **곡선 경로.** `ui/map-route.mjs` 의 Catmull-Rom은 표시 전용.
   시뮬레이터 이동, 승인 신청의 legs, `first_breach`, 원장·에이전트 구조는 변경 없음.
   곡선은 경유점 사이에서 원래 직선과 다를 수 있으며 공역 판정에 사용하지 않음.
6. 사용자가 "격자로 2d마냥 표현하지 말라"고 두 번 말함. 지금은 같은 등급끼리 합쳐
   `fill-extrusion` 으로 세워놨는데(금지=200m 벽, 허용=천장 높이 판), **눈으로 확인 안 됨.**
   초기 zoom에서도 입체 층이 나오도록 minzoom 제한을 제거했고, 지원하지 않는
   feature별 opacity 표현식을 상수로 수정함.
   [MapLibre 속성 명세](https://maplibre.org/maplibre-style-spec/layers/#fill-extrusion-opacity) 참고.
   연결 가능한 브라우저가 없어 실제 배치·가독성·WebGL 렌더링 검증은 남음.

### B. 검증 못 한 것
1. **Docker 를 한 번도 못 돌렸다.** 이 기계에서 데몬이 안 붙었음(`open -a Docker` 실패).
   `docker compose config` 는 6개 조합 다 통과하지만 **빌드와 실행은 미검증.**
   - `compose.yaml` (기본) — 로컬 프로세스로는 동작 확인함
   - `compose.view.yaml` (Skybrush 3D) — **완전 미검증.** `npm run bundle` 이 될지 모름
   - `compose.sitl.yaml` (PX4 SITL 6대) — **완전 미검증**
   - `compose.gpu.yaml`, `compose.edge.yaml`, `compose.mac.yaml` — 미검증
   - 2026-09-09 재확인: 샌드박스 밖에서 `open -a Docker` 는 종료 코드 0이었으나,
     이후 `docker info` 는 계속 `Cannot connect to the Docker daemon` 으로 실패.
     따라서 이번에도 이미지 빌드·컨테이너 실행, Skybrush 번들, PX4 6대 연결은 검증 못 함.
   - config 재검증 6조합 통과: 기본, 기본+view, 기본+sitl, 기본+sitl+gpu,
     기본+edge, 기본+mac. GPU는 sitl 위에 얹어야 하며 기본+gpu만으로는 fleet 정의가 없음.
   - 기존 로컬 서버의 `/compare`, `/state`, `/map.html`, `/map-route.mjs` 응답 확인.
     지도 파일은 수정본과 동일. 기존 API 응답은 양쪽 기단 3대씩이며 확인 시점에는
     route가 모두 비어 있었음. 실제 비행 중 화면 검증을 대체하지 않음.
   - `scripts/dev.sh` 가 직접 기단에도 `attache.agent.loop` 를 띄우던 오래된 배선을
     `direct_agent.loop` / `TRANSPORT=http` 로 수정함. 기존 서버와 포트가 겹쳐
     새로 시작한 개발 프로세스는 종료했고, 수정한 스택의 완전한 기동 검증은 남음.
2. **Nemotron 을 한 번도 호출 안 했다.** `NEBIUS_API_KEY` 가 없어서 전부 규칙 기반으로 돌았음.
   `attache/llm/client.py` 는 스텁 없이 단위 시험만 됨.
3. **Tavily 미연동.** 대회 상 하나가 걸려 있음(Best Use of Tavily $3,000).

### C. 설계상 남은 구멍
1. **물리 이동은 직선 구간 기준이다.** 지도 곡선 렌더링은 완료(위 A-5).
   실제 곡선 비행을 도입하려면 별도 설계가 필요하며 이번 작업 범위는 표시뿐임.
2. **배달 반려율이 84%** (78 반려 / 14 완료). 실제 FAA 데이터로는 브루클린 기지에서
   맨해튼 대부분이 도달 불가. 이건 진짜 발견이지만 데모로는 기단이 무능해 보인다.
   기지를 옮기거나(퀸스/저지시티), 배달지를 도달 가능 구역으로 좁히거나,
   "허가 신청(LAANC)" 개념을 넣어 반려 대신 승인 대기로 만드는 선택이 필요.
3. **속도가 실제의 약 10배.** `SPEED_PER_TICK = 0.55`. 사용자가 "천천히 움직여야지"라고 함.
   느리게 하면 데모 한 판이 너무 길어짐. 틱 간격(`TICK_SECONDS`)과 같이 조절해야 함.
4. **중재(arbiter)가 거의 안 불린다.** 충전대 2자리에 기체 3대인데 실제 경쟁이 드묾.
   Ultra 티어를 쓰는 유일한 자리라서, 데모에서 한 번은 반드시 걸리게 시나리오를 잡아야 함.
5. **`attache/agent/planner.py` 의 운영사 공역 사본이 런타임과 같은 파일을 읽는다.**
   원래 의도는 "운영사 사본은 낡을 수 있다"였음. 일부러 낡게 만들면
   "계획기는 된다고 했는데 런타임이 거절" 이라는 진짜 시나리오가 생김.
6. **`what_if.py` 의 기종 필터가 엉성하다.** `--model` 을 주면 모든 자산에 같은 기종을 씌움.
   원장에 기체 기종이 안 남아서 그럼. `Proposal` 에 기종을 넣거나 원장에 자산 상태를 같이 남겨야 함.

---

## 5. 전체적으로 손봐야 할 것

1. **README 가 뒤처졌다.** 잠실/로보택시 시절 서술이 남아 있고, 배달 드론·뉴욕·경로 협상
   구조가 반영 안 됨. 두 세계 표 숫자도 옛날 것.
2. **`sim/world.py` 가 774줄로 비대하다.** 세계·조종장치·점수판·공역 판정이 한 파일에 있음.
3. **테스트가 52초 걸린다.** A* 라우터 때문. `tests/test_two_worlds.py` 가 1200틱 × 2세계.
4. ~~`ZONE` 좌표 확인 필요~~ → **고침.** 이름 변경 때 `forbid_resource` 가 없어진
   `pad:P1` 을 가리켜 동적 구역이 아무 효과도 없었음. `bay:A` 로 고치고 폴리곤을
   브루클린 기지 위로 옮김. 이런 종류의 잔재가 더 있을 수 있으니 이름 바꾼 자리를 훑을 것.
5. `configs/` 가 11,901줄인데 대부분 FAA/OSM 데이터. `.gitignore` 에 넣고 fetch 스크립트로
   재생성하게 할지 결정 필요. (지금은 커밋되어 있어서 클론하면 바로 돎 — 장점)
6. **미검증 문서 주장.** README 에 "docker compose up 하면 된다"고 적혀 있는데 미검증.
   Docker 를 돌려보기 전까지는 그 문장에 단서를 달아야 함.

---

## 6. 참고 — 조사해서 확정된 사실들

- **Docker Desktop 은 Apple Silicon 에서 리눅스 컨테이너에 GPU 를 안 넘긴다.**
  컨테이너 안에서 3D 를 그리는 방법(Gazebo/Webots/Unreal/Isaac)이 전부 여기서 죽음.
  그래서 Skybrush(서버는 순수 파이썬, 렌더링은 브라우저)를 골랐음.
- Webots 는 linux/arm64 빌드가 세상에 없음. 최신 Gazebo 는 공식 도커 이미지가 없음.
- `px4io/px4-sitl-gazebo:v1.18.0-beta2` 는 amd64+arm64 멀티아치 (Docker Hub 에서 직접 확인).
- Isaac Sim / Project AirSim 은 x86 리눅스 + NVIDIA 전용. Mac 에서 불가.
- 표준 근거: ASTM F3269(런타임 보증), ASTM F3548(UTM), EASA AI 컨셉페이퍼 Issue 03(DAL C 상한),
  FAA Part 108(운영자 책임), ED-269/ED-318(공역 데이터 모델).
