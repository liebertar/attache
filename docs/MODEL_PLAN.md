# 모델은 어디에, 런타임은 무엇을 잡나 — 한 장

## 1. 어떤 NVIDIA 모델을 어디에 붙이나

| 티어 | 모델 (NVIDIA 오픈 가중치) | 어디서 도나 | 하는 일 | 권한 | 상태 |
|---|---|---|---|---|---|
| **Nano** | Nemotron 3 Nano 30B-A3B (Dec 2025) · 프로덕션은 Nebius의 Nemotron 3.5 Lightning 30B-A3B(Aug 2026) | 드론마다 1개 에이전트 프로세스. 로컬: Ollama `nemotron-3-nano`(24 GB q4) · 프로덕션: Nebius Token Factory | ① 신청서 작성(무엇을·왜) ② **경로 초안**(경유점+고도) ③ 거절 사유 받아 재초안 ④ (TODO) 거절 뒤 대응 선택(고도↑/대기) ⑤ (TODO) 메모·공지 문장 읽고 신청으로 | **제안만.** 양식이 아니면 버리고 규칙·A*가 대신 | ①②③ 코드 들어옴(검증·커밋 중) |
| **Super** | Nemotron 3 Super 120B-A12B (Mar 2026) `nvidia/nemotron-3-super-120b-a12b` | 런타임 옆(호출만, 호스팅 안 함) | ① 거절 사유를 관제사 문장으로 ② (TODO) 문법으로 못 읽는 NOTAM 문장을 규칙 스키마로 구조화 → **사람 확인 뒤** 적용 | 설명·초안. 규칙을 조이는 쪽만 즉시, 푸는 쪽은 사람 | TODO |
| **Ultra** | Nemotron 3 Ultra 550B-A55B (Jun 2026) `nvidia/Nemotron-3-Ultra-550b-a55b` | 런타임 옆 | ① 겹치는 신청 중 **통과한 것들 사이에서** 하나 고르기 + 한 줄 이유 ② (TODO) 야간 원장 재생 감사 보고서 | 고르기만. 목록 밖 답은 버리고 규칙(blast>battery>filed) | ① 코드 들어옴 |

**붙이는 방식** (`attache/llm/client.py`): OpenAI 호환 `/chat/completions` 하나. `LLM_BASE_URL` + `NEBIUS_API_KEY` (Nebius) 또는 `http://localhost:11434/v1` (Ollama).
생각(thinking) 모델의 `reasoning` 필드·`<think>` 처리, JSON 강제, 타임아웃(에이전트 6 s), 답이 없거나 틀리면 **항상 규칙 폴백**.
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
| **경로 사이 분리** | 승인된 회랑을 4D 의도(폴리곤 30 m 확장 · 고도 ±25 m · 시간 ±30틱)로 보관, 새 신청이 남의 의도와 공간·시간 교차하면 거절(ASTM F3548 전략적 비충돌). 같은 착륙장·같은 시간도 거절 | `CROSSES drone-03` | **TODO (다음 워크플로)** |
| 이륙·승강 기둥 | 출발점 수직 상승, 꼭짓점 고도 변경, 착륙 하강도 구간으로 판정 | `NO ROOM TO LIFT OFF` | TODO |
| NOTAM 문장 → 규칙 | FAA 형식 문장을 문법으로 시간 창 있는 Volume으로. 못 읽는 것만 Super → 사람 | 배너 | TODO |
| 원장 맥락 | 판정 틱·공역 판본·정책·의도 id·돌린 검사 목록 기록 | — | TODO |

**한 줄:** 에이전트(모델)는 그리고 신청한다 → 런타임(코드)은 판정하고 기록하고 실행한다 → 화면은 누가 썼고 어느 규칙이 잡았는지 보여준다.
