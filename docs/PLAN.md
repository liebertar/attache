# 고도화 계획 · 체크리스트 (2026-09-09, 09-10 갱신)

사용자가 말한 것을 빠짐없이. 완료는 `[x]`, 눈으로 확인한 것은 `👁`, 시험으로 확인한 것은 `✓`.
런타임은 **검증만** 한다(경로·경로 사이 고도·건물·착륙 자리). 기획·자율주행은 기업(에이전트) 몫.
모델은 눈에 보이되 결정권이 없다. 이 문서의 원칙은 `docs/REQUIREMENTS.md` G장을 따른다.

## A. 화면 (사용자 지시)
- [x] 지면 선 없이 공중 회랑만, 회랑은 기체 아래 얇게 👁
- [x] 구역은 바닥 채색, 건물보다 아래 👁
- [x] 드론 확대, 네모 상자 스택(적재·하역·수거에 따라 늘고 줆) 👁
- [x] 기체 옆 상태 문구(LOADING 3/6 · UNLOADING · PICKING UP · LANDING · RETURNING · READY) + 고도 👁
- [x] 돈·배터리 방전·turn back 표현 제거, 범례 정리 👁
- [x] 헤더 카드 안에서 버튼이 밖으로 나가지 않게 👁
- [x] 착륙장 표지 "DRONE LANDING AREA", 땅에 붙은 원(지도와 같이 기울어짐) 👁
- [x] 거절 카드·기록 클릭 → 그 기체로 이동 / RESTART → 창고로 이동
- [x] 카메라 조작은 단축키(우클릭 드래그·Shift+화살표·N·H·1~4), 하단 범례에 한 줄
- [x] 출처 표시: 헤더 한 줄(`Nemotron nano · via ollama`) + 기록 태그(`[model] delivery route · nano`) 👁 — `APPROVED · <규칙>` 문구는 미확인
- [x] TOWER ADVISORY 카드(아래 가운데 12초, 불가 선택지 취소선, 고른 것 표시, 클릭 → 그 기체) 👁✓
- [x] NOTAM 배너 세 상태: 문법이 읽음 / 모델이 읽고 사람 대기(`held`) / 사람이 확인·창 대기. 적용된 런타임 공지는 바닥 채색, 보류는 안 칠함 👁✓
- [x] `human` 판정 줄은 승인 회랑으로 안 그림(HUMAN), `?rt=&sim=` 포트 덮어쓰기, favicon ✓
- [ ] `awaiting_human` 카드를 지도에 (지금은 `index.html` 승인 화면에만)
- [x] 다른 기체 회랑과의 충돌 거절을 그 자리에 (CROSSES drone-03, 상대 회랑 깜빡임) ✓ 물림·출발점/끝점 거절·NOTAM 배너(누가 읽었나)·서 있는 기체 위 착륙 행까지 화면에

## B. 순환 (사용자 지시)
- [x] 4대, 마당 제 자리 한 줄, 창고에서 6개 적재 → 착륙장 두 곳 3개 하역·2개 수거 → 복귀 하역 → 재적재 ✓👁
- [x] 출발은 항상 지상, 승인 깜빡임 끝나고 이륙 ✓👁
- [x] 공중 정지 없음(3200틱에 2틱) ✓
- [x] 충전대 순환 제거(마당 ↔ 이륙장 짧은 비행 없음), 착륙대 충돌 행 제거
- [x] 배달지는 지정 착륙장 30곳(맨해튼 17 + 센트럴파크 북쪽·할렘 7 + 브루클린·퀸스 6), 전부 둘레 50m·왕복 경로 확인 ✓👁
- [x] 판의 첫 배달: drone-01 → 센트럴파크 북쪽 110번가, drone-03 → 모닝사이드 (직선이 미드타운 0ft 격자에 걸려 거절 → 우회) 👁
- [x] 두 번째 정차는 첫 정차 가까운 곳 (한 판에 한 바퀴 이상)

## C. 판정 (런타임)
- [x] 건물 = 화면 타일 건물(OSM, 40m 이상 14,837동) ✓
- [x] 옥상 위 이격 50m, 옆 이격 10m(건물)/40m(격자·구역), 착륙 둘레 50m ✓
- [x] 구간 고도 = 아래 가장 높은 옥상 + 50m(최소 40m, 최대 120m): 강 위 40m, 저층 위 90~118m, 탑은 옆으로 ✓
- [x] 꼭짓점에서 제자리 승강(내려가면서 앞으로 나가지 않음)
- [x] 회랑이 타일 건물을 지나는 지점 0건 (playwright 대조) 👁
- [x] 경로 사이 분리: 승인된 회랑끼리 공간·시간(4D) 충돌 검사 (F3548 전략적 비충돌), 수평 30m·수직 25m·시간 ±30틱 ✓ (`runtime/intents.py`, 씨앗 7 에서 런타임 0 / 직결 >0)
- [x] 운영사 해결 사다리: 충돌이면 +30m 또는 출발 지연으로 재신청 (에이전트 쪽 규칙) ✓ (`loop._resolve_traffic`, 하네스 거울)
- [x] 이륙 기둥·꼭짓점 승강 구간도 판정 (경로 끝점뿐 아니라 수직 구간) ✓ (`geo.vertical_column`, blocked_kind takeoff/column)
- [x] NOTAM 문장 → 시간 창 있는 Volume (문법 파서, 못 읽는 문장만 Super가 구조화 → 사람 확인) ✓ (`core/notam.py`, `runtime/notices.py`, `/state.notices`)
- [x] 원장에 판정 맥락(틱·공역 판본·정책·의도 id·검사 순서) 기록 ✓ (`LedgerEntry.context`, 중복 거절도 기록)
- [x] 문법이 못 읽는 공지(MEDEVAC 1350~2100틱, 0.5 NM) → Super 구조화 → `held` 보류 → 사람 승인 뒤에만 적용, 창 닫히면 `notice_lapsed`; 읽기는 세계 스레드 밖(틱 안 멈춤, `NOTICE_TIMEOUT_S`) ✓👁
- [x] 관제 권고: 같은 막힘 3번째 거절에 코드가 선택지(대기·+30m·공지 창·거절·사람)를 만들어 판정, Super 는 목록 안에서 하나 고르기 + 요약만. 실행·잠금 안 바꿈 ✓👁
- [x] `human` 판정마다 원장 항목(pending → 승인/거절/lapsed), 카드는 판마다 닫힘(`card_lapsed`) ✓
- [x] 비행별 보고서 `GET /ledger/report?asset=&format=json|md` — 원장 파일 전체에서, 중복 거절·실행 실패 따로 ✓
- [x] 로컬 30B 는 0.5 NM 원을 잘못 놓음 → 승인→회수 경로는 authored fixture(`notices_super.json` reference)로 검증 ✓ (사람 게이트가 필요한 이유)

## D. 모델 (Nemotron, 결정권 없음)
- [x] Ollama 로컬: `nemotron-3-nano`(30B-A3B, 24GB) + `nemotron-3-nano:4b`(2.8GB). 드론마다 서버 하나 `scripts/ollama_fleet.sh start 4`(Ollama 가 슬롯 1을 강제) ✓
- [x] LLM 클라이언트: reasoning 필드·<think> 처리, JSON 강제, 타임아웃, 기록(fixture) ✓
- [x] 모델 ID 정리(Nebius: Super 120B-A12B, Ultra 550B-A55B, Nano/3.5 Lightning), `.env.example`(Nebius + Ollama) — Nebius 쪽 id 는 키가 없어 목록 확인 못 함(`scripts/llm_probe.py`)
- [x] 드론마다 Nano가 경로 초안(경유점+고도)을 그림 → 런타임 판정 → 거절이면 사유 받아 재초안 → 두 번 안 되면 A* (E5b) ✓ — `attache/agent/drafter.py`, 원장 `params.drafter`
- [x] 초안은 거절 순간에 작업 스레드에서 시작(예산 60초는 거절 시각부터, 화면 5.6초는 따로), 재질문에 장애물 id·옥상·필요 고도·지날 쪽 하나, 천장 칸 안 건물도 GO AROUND, 기체가 움직였으면 첫 점 재고정 ✓
- [x] 4B 의 `alt_ma` 키 오타 등 별칭 허용(값 검사는 그대로), 마당에 선 기체의 `charge` 신청서 차단(`propose.possible_now`) ✓
- [x] 중재(Ultra): 통과한 후보 중 번호 + 한 줄 이유, 배경 스레드 밖에서 ✓ (`decision.detail.arbiter_reason`, 3초 느린 중재도 틱을 안 멈춤)
- [x] 모델 호출 시험: 녹음된 답으로 오프라인, 임의·악의 경유점 fuzz → 실행된 경로는 항상 위반 0 ✓ (`tests/fixtures/llm`, `ChaosDraftsNeverFlyTest` 1500틱)
- [x] 실제 호출 증거: 4B 가 그린 경로가 판정을 지나 실행(수정 전 65분 9건, 수정 후 10분 6건), 캡처 `live-approved-nano-corridor.png` 👁 — REQUIREMENTS 0-8 라이브 2
- [ ] 4B 로 맨해튼 10km 횡단 초안 승인 — 지금 0, 전부 A* 폴백. 재질문 승인도 0
- [ ] Nebius Token Factory 로 한 판 — 키 없음. 데모 배선은 이쪽(Super 120B·Ultra 미검증)

## E. 문서·검증
- [x] REQUIREMENTS 0장 갱신(0-5 제품 방향·0-8·0-10·0-7), README 실행법(Ollama 함대·Nebius), MODEL_PLAN·HOW
- [x] 전체 시험: python 276(skip 4) · node 34 ✓
- [ ] 마지막 캡처: 적재 → 거절 → (거절 순간 초안) → 우회 → 착륙 → 수거 → 복귀 한 흐름, `source: super` 권고 카드 라이브, KR 문구
- [ ] 사용자 판정: 예산 코드(drone-04 가 판 중간에 $320 한도 → 전부 `human` 카드), 구역 폐쇄 시나리오, 뉴저지 착륙장
