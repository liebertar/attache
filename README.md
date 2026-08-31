# Attaché

**Agents can propose. Only the runtime can commit.**

*[English](README.en.md)*

실제 자산 — 기계, 계정, 노드 — 에 묶여 오래 살아가는 에이전트들을 위한 **결정 권한 런타임**.

에이전트는 관찰하고 제안한다. 락을 잡고, 충돌하는 제안을 중재하고, 각 제안을 금액과 폭발 반경으로
표현된 권한 봉투에 비추어 검사하고, 통과한 것만 실행하고, 누가 무엇을 왜 커밋했는지 남기는 것은
런타임의 일이다.

**에이전트에는 `execute()`가 없다.** 현실로 나가는 코드 경로는 정확히 하나이고, 그 경로는 런타임이 소유한다.

---

## 왜 프레임워크가 아니라 런타임인가

신경망 정책은 비결정적이다. PLC 래더 로직처럼 출력을 인증할 방법이 없다. 그런 정책을 실제 돈이나
실제 모터에 붙여 배포하는 길은 하나뿐이다 — **정책을 제약하려 하지 말고, 무엇이 커밋될 수 있는지를 제약한다.**

지금 그 층이 비어 있다.

| 층 | 답하는 질문 |
|---|---|
| MCP / A2A | 에이전트가 도구에 어떻게 닿는가 |
| NVIDIA NeMo Relay | 호출 하나가 실행되는 동안 무엇을 하는가 — 차단·재시도·라우팅·추적 |
| **Attaché** | **누가 제안할 수 있는가, 충돌하면 누가 이기는가, 얼마까지 자동인가, 누가 커밋했는가** |
| Policy pack | 도메인별 숫자 |

가드레일은 나쁜 호출을 막는다. Attaché는 **정당한 제안들 중 하나를 고른다.**

---

## 아키텍처

```
        configs/robot.yaml            configs/finance.yaml
                 └──────────┬──────────────────┘
                            │  자산 · 서브시스템 · 자원 · 권한
   ┌────────────────────────▼────────────────────────┐
   │                     runtime                     │
   │                                                 │
   │   lock table        배타적 자원 점유표            │
   │   arbiter           제안이 충돌할 때만 기동        │
   │   authority         cost_usd · blast_radius     │
   │   commit            바깥으로 나가는 단 하나의 경로  │
   │   ledger            추가 전용 · 근거 체인          │
   └───┬───────────────────────────────────┬─────────┘
       │                                   │
  asset agents                         adapters
  규칙 → Nano → Super → 제안             gpio · payments · kubectl
       │                                   │
   NeMo Relay                          현실
   스코프 · 추적 · 미들웨어                모터가 멈춘다
                                       환불이 나간다
       │
   Tavily
   클러스터 안에서는 알 수 없는 것
```

| 컴포넌트 | 책임 |
|---|---|
| `runtime` | 락, 중재, 권한, 커밋, 원장 |
| `agent` | 규칙으로 관찰하고, 필요할 때 모델로 올리고, 제안한다. 실행하지 않는다 |
| `adapters` | 현실을 건드리는 유일한 코드. 커밋만이 호출한다 |
| `configs` | 도메인이 무엇이고 한도가 얼마인지 |
| `ui` | 한도를 넘었을 때 사람이 보는 승인 화면 |

---

## 도메인 독립

런타임은 모터도 환불도 모른다. 도메인은 설정이다.

```
attache run configs/robot.yaml      # 모터가 멈춘다
attache run configs/finance.yaml    # 환불이 승인된다
```

같은 바이너리. 같은 커밋 경로. 같은 원장.

---

## 제안 하나가 지나가는 길

```
자산 에이전트 ── 규칙으로 상시 감시, 모델 호출 없음
     │  이상 → 소형 모델 → 진단 (+ Tavily로 외부 근거)
     ▼
  propose(action, cost_usd, blast_radius, evidence)
════════════ 런타임 경계 — 에이전트에는 execute() 가 없다 ════════════
     ▼
  자원 락 확인 ──점유 중──► 중재 (대형 모델, 충돌할 때만)
     ▼
  권한 검사 ──한도 초과──► 사람 승인
     ▼
  커밋 ──────────────────► 현실 (GPIO / API / kubectl)
     ▼
  ledger.jsonl  { committed_by: runtime, from, evidence, limit_checked }
```

---

## 에스컬레이션 사다리

대부분의 서브시스템은 모델을 부르지 않는다. 비용은 나중에 최적화할 것이 아니라 설계 제약이다.

| 티어 | 언제 도는가 | 모델 |
|---|---|---|
| 규칙 | 항상 | 없음 |
| 서브시스템 | 규칙이 트립할 때 | Nemotron 3 Nano — 30B 총 / 3B 활성 |
| 자산 | 이상 판정 시 | Nemotron 3 Super — 100B / 10B 활성 |
| 중재 | 제안이 충돌할 때만 | Nemotron 3 Ultra — 550B / 55B 활성 |

Nebius Token Factory에서 서빙.

---

## 원시

`asset` · `subsystem` · `signal` · `resource` · `proposal` · `commit` · `escalation` · `authority`

감사 추적은 아홉 번째 원시가 아니라 **불변식**이다. 모든 커밋은 자신을 만든 체인을 함께 들고 다닌다.

---

## 상태

초기. Nebius × NVIDIA Global AI Hackathon 2026, Physical AI 트랙.
대회 관련 정리는 [HACKATHON.md](HACKATHON.md).

## 라이선스

Apache-2.0
