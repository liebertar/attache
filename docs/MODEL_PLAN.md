# 모델은 어디에, 런타임은 무엇을 잡나 — 한 장

## 1. 어떤 NVIDIA 모델을 어디에 붙이나

| 티어 | 모델 (NVIDIA 오픈 가중치) | 어디서 도나 | 하는 일 | 권한 | 상태 |
|---|---|---|---|---|---|
| **Nano** | Nemotron 3 Nano 30B-A3B (Dec 2025) · 프로덕션은 Nebius의 Nemotron 3.5 Lightning 30B-A3B(Aug 2026) | 드론마다 에이전트 프로세스 1 + **로컬 Ollama 서버 1**(`scripts/ollama_fleet.sh`, 11435.., `nemotron-3-nano:4b` 3 GB 씩 — Ollama 가 이 계열에 슬롯 1을 강제해 서버 하나를 넷이 나누면 초안이 잘림) · 프로덕션: Nebius Token Factory | ① 신청서 작성(무엇을·왜) ② **경로 초안**(경유점+고도) ③ 거절 사유(장애물·필요 고도·지날 쪽) 받아 재초안 ④ 거절 뒤 대응은 규칙 사다리(+30 m → 지연)가 함, 모델 선택 없음 ⑤ (TODO) 메모·공지 문장 읽고 신청으로 | **제안만.** 양식이 아니면 버리고 규칙·A*가 대신 | ①②③ **됨, 라이브 확인** — 초안은 거절 순간에 시작(예산 60 s 는 거절 시각부터), 4B 가 그린 경로가 판정을 지나 실행됨(수정 전 7분 2 · 65분 9 · 수정 후 10분 6). 솔직히: 브루클린 짧은 경로만, 맨해튼 10 km 횡단은 A*, 재질문 승인 0. fixture `drafts_nano.json` 에 실주행 4B 통과 둘 |
| **Super** | Nemotron 3 Super 120B-A12B (Mar 2026) `nvidia/nemotron-3-super-120b-a12b` | 런타임 옆(호출만, 호스팅 안 함). 로컬 대역: 11434 의 `nemotron-3-nano:latest`(30B), 런타임 프로세스에만 | ① **관제 권고**(같은 막힘 3번째 거절에 코드가 만든 합법 선택지 중 하나 + 400자 요약) ② 문법으로 못 읽는 NOTAM 문장을 규칙 스키마로 구조화 → **사람 확인 뒤** 적용 | 설명·초안. 목록 밖 답은 버림. 규칙을 조이는 쪽만 즉시, 푸는 쪽은 사람 | ①② **코드 됨.** 로컬 대역 라이브: 공지(MEDEVAC 문장)를 8.8 s 에 읽어 보류 → 사람 없이 lapsed(수정 전엔 4판 중 1판, 20 s 타임아웃); 권고는 괄호 관용 뒤 `source: super`(수정 전 160 중 155 가 `"[hold]"` 로 규칙 폴백). 대역이 0.5 NM 원을 잘못 놓아 승인→회수는 authored fixture 로만. **Nebius 120B 미검증(키 없음)** |
| **Ultra** | Nemotron 3 Ultra 550B-A55B (Jun 2026) `nvidia/Nemotron-3-Ultra-550b-a55b` | 런타임 옆 | ① 겹치는 신청 중 **통과한 것들 사이에서** 하나 고르기 + 한 줄 이유 ② (TODO) 야간 원장 재생 감사 보고서 | 고르기만. 목록 밖 답은 버리고 규칙(blast>battery>filed) | ① 코드 됨(라이브는 `MODEL_ULTRA` 없이 규칙) ② 비행별 보고서 `GET /ledger/report` 는 코드로 있음, 모델 감사는 TODO |

**붙이는 방식** (`attache/llm/client.py`): OpenAI 호환 `/chat/completions` 하나. `LLM_BASE_URL` + `NEBIUS_API_KEY` (Nebius) 또는 `http://localhost:11434/v1` (Ollama).
생각(thinking) 모델의 `reasoning` 필드·`<think>` 처리, JSON 강제, 타임아웃(신청서 6 s · 초안 `DRAFT_TIMEOUT_S` 60 s · 공지 `NOTICE_TIMEOUT_S` 60 s · 권고 12 s),
답이 없거나 틀리면 **항상 규칙 폴백**. 기체마다 다른 서버는 `LLM_PER_ASSET_URLS`(dev.sh). 초안·공지·권고 호출은 전부 데몬 스레드라 틱을 안 멈춥니다.
시험은 실제 Ollama 답을 녹음한 fixture(`tests/fixtures/llm/`)로 오프라인, 그리고 무작위·악의 경유점 fuzz.

## 2. 런타임이 잡는 것 — 전부 결정적 코드, 판정 함수 하나 (`attache/core/geo.first_breach`)

| 잡는 것 | 어떻게 | 거절 문구 | 상태 |
|---|---|---|---|
| 건물 관통 · 옥상 이격 50 m | 구간 선분 ↔ 건물 폴리곤 교차(화면 타일 건물 14,837동, 40 m↑). 건물은 옥상+50 m까지 막힘 | `leg 3 enters BUILDING 62 m` / `옥상 위 40 m (이격 50 m 필요)` | 됨 |
| 옆 이격 | 선분–폴리곤 최소거리: 건물 10 m, FAA 격자·폐쇄 구역 40 m | `건물 41 m 에 3 m 로 접근` | 됨 |
| FAA 격자 천장 · 0 ft 칸 | 구간 고도 > 칸 천장(30/61/91/122 m) 거절, 0 ft 칸은 진입 금지. 칸 밖은 Part 107 기본 400 ft | `KLGA 격자 100ft 허용 고도 초과` / `FAA GRID · NO FLIGHT` | 됨 |
| 착륙 자리 | 경로 끝점 둘레 50 m 안에 건물·금지 구역 있으면 거절 | `NO ROOM TO LAND · BUILDING 53 m` | 됨 |
| 나는 중 도착한 규칙 | 구역 폐쇄 공지 → 지나던 승인 경로 회수, 가장 가까운 바깥으로 내보냄, 새 경로는 거절. 감항성 지시 → 그 기종 fly_route 거절 | `RECALLED`, `banned by ad-…` | 됨 |
| 승인 없는 실행 | 실행 코드는 런타임 안 `commit()` 한 곳. 원장에 먼저 적고 실행. 직결 세계는 기록 자체가 없음 | 점수판 `Acts with no record` | 됨 |
| **경로 사이 분리** | 승인된 회랑을 4D 의도(폴리곤 30 m + 항법 오차 10 m · 고도 ±25 m · 시간 ±30틱)로 보관, 새 신청이 남의 의도와 공간·시간 교차하면 거절(ASTM F3548 전략적 비충돌). 같은 착륙장·같은 시간도 거절 | `CROSSES drone-03` | 됨 (4e6de07, `runtime/intents.py`; 씨앗 7 런타임 0 / 직결 5) |
| 이륙·승강 기둥 | 출발점 수직 상승, 꼭짓점 고도 변경, 착륙 하강도 구간으로 판정 | `NO ROOM TO LIFT OFF` | 됨 (`geo.vertical_column`, 검사 이름 `columns`) |
| NOTAM 문장 → 규칙 | FAA 형식 문장을 문법으로 시간 창 있는 Volume으로. 못 읽는 것만 Super → `held` 보류 → **사람 승인 뒤에만** 걸림, 창이 닫히면 `notice_lapsed` | 배너 `read by the rule grammar` / `waiting for a person` | 됨 (`core/notam.py`, `runtime/notices.py`; 둘째 공지 MEDEVAC 1350–2100틱은 문법이 못 읽는 문장) |
| 원장 맥락 | 판정 틱·공역 판본·정책·의도 id·돌린 검사 목록. `human` 판정·권고·보류 공지도 항목을 열고 닫음(pending → 확정/`lapsed`) | — | 됨 (`LedgerEntry.context`; `GET /ledger/report` 가 파일에서 비행별로 접음) |
| 거절이 쌓인 기체 | 같은 막힘 3번째 거절에 권고(advisory) 한 장 — 선택지는 코드가 만들어 판정, 실행·잠금은 안 바꿈 | `TOWER ADVISORY` 카드 | 됨 (`runtime/advisory.py`, 정보일 뿐 — "잡는 것" 은 아님) |

**한 줄:** 에이전트(모델)는 그리고 신청한다 → 런타임(코드)은 판정하고 기록하고 실행한다 → 화면은 누가 썼고 어느 규칙이 잡았는지 보여준다.
