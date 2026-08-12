# 토스증권 Open API 실거래 연동 계획

**상태:** 기획 전용. MVP 1에는 토스증권 API 호출이나 실거래 코드가 없다.

**확인 기준일:** 2026-08-12

## 1. 공식 API 전제

토스증권 Open API는 국내(KRX)·미국 주식에 대해 시세, 종목·시장 정보, 계좌,
보유자산, 일반 주문과 조건주문을 제공한다. 인증은 OAuth 2.0 Client Credentials
Grant이고, 계좌·자산·주문 요청에는 `X-Tossinvest-Account` 헤더가 추가로 필요하다.

현재 공식 가이드의 연동 방식은 REST 전용이다. 시세 문서도 WebSocket을 추후 지원
예정으로 안내하므로, 초기 설계는 REST 폴링과 주문 상태 재조회에 맞춘다.

공식 자료:

- [토스증권 Open API 안내](https://home.tossinvest.com/ko/open-api)
- [Open API 가이드](https://developers.tossinvest.com/docs)
- [LLM용 공식 소스 안내](https://developers.tossinvest.com/llms.txt)
- [공식 OpenAPI JSON](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)

## 2. 보안 경계

```text
Mobile approval UI
        |
        v
Investment Town Control API
        |
        v
Deterministic risk gate -> Toss broker adapter -> Toss Securities Open API
```

- `client_id`, `client_secret`, 액세스 토큰, 계좌 식별자는 서버 비밀 저장소에만 둔다.
- 모바일 앱과 LLM Agent에는 원본 비밀값과 주문 API 호출 권한을 주지 않는다.
- Agent는 `TradeProposal`만 만들며 Broker Adapter를 직접 호출하지 못한다.
- 실거래 스위치는 기본 `false`이고 paper/shadow/live 환경을 물리적으로 분리한다.
- 모든 승인과 주문 요청·응답은 비밀값을 제거한 뒤 변경 불가능한 감사 로그에 남긴다.

## 3. 주문 실행 순서

1. Agent 연구 결과를 구조화된 `TradeProposal`로 고정한다.
2. 가격 시각, 장 운영 시간, 종목 경고, 상·하한가를 새로 조회한다.
3. 매수 가능 금액 또는 매도 가능 수량과 수수료를 조회한다.
4. 결정론적 위험 엔진이 종목·일일 손실·포지션·주문 금액 한도를 검사한다.
5. 모바일 화면에서 종목, 방향, 수량, 예상 금액, 수수료, 근거와 위험을 확인한다.
6. 사용자 승인 후 서버가 고유 `clientOrderId`와 승인 스냅샷 해시를 생성한다.
7. Broker Adapter가 `POST /api/v1/orders`를 한 번 호출한다.
8. 응답이 불명확하면 재주문 전에 `clientOrderId`/주문 내역으로 상태를 조정한다.
9. 체결·거부·취소 상태를 주기적으로 조회해 내부 주문 원장과 대조한다.

## 4. 토스 API 대응 규칙

- `clientOrderId`를 멱등키로 저장하고 같은 키에 다른 주문 내용을 재사용하지 않는다.
- `429`는 `Retry-After`와 rate-limit 응답 헤더를 따르고 지수 백오프와 jitter를 쓴다.
- 주문 그룹은 통상 초당 6회, 09:00~09:10 KST에는 초당 3회로 더 낮으므로 자체
  토큰 버킷을 공식 응답 한도보다 보수적으로 둔다.
- 네트워크 단절·`500`에서 주문 생성 요청을 무조건 재시도하지 않는다. 먼저 주문
  내역을 대조해 중복 주문 가능성을 제거한다.
- 1억원 이상 주문에 필요한 `confirmHighValueOrder=true`는 별도의 고액 주문 승인
  절차 없이는 절대 자동 설정하지 않는다.
- 이미 체결·취소·정정된 주문의 `409`는 재시도하지 않고 원장을 동기화한다.
- 주문 가능 시간, 거래 제한 종목, 부족한 주문 가능 금액/매도 수량 등 `422`는
  사용자에게 구체적인 거부 사유로 표시한다.
- 서버 응답의 `requestId`와 `X-Request-Id`를 감사 로그와 장애 문의 자료에 보존한다.

## 5. 지원 범위 순서

### Phase 0 — 현재

- Paper Broker만 사용
- 토스 자격증명 미사용
- 실거래 코드는 존재하지 않음

### Phase 1 — Read-only

- OAuth 토큰 관리
- 계좌·보유 종목·매수 가능 금액 조회
- 시세·시장 캘린더·종목 경고 조회
- 내부 포트폴리오와 토스 계좌 대조

### Phase 2 — Shadow Live

- 실제 시세와 계좌를 이용해 주문 후보만 생성
- 토스 주문 API는 호출하지 않음
- paper 주문과 실제 시장 결과 비교

### Phase 3 — Human-approved Live

- 지정가 주문부터 시작
- 주문당·일일 금액 한도
- 매 주문 모바일 승인
- 생성·조회·취소와 원장 대조
- kill switch와 자격증명 즉시 폐기

### Phase 4 — 제한 자동화

- 충분한 shadow/live 운영 기록과 별도 보안 검토 이후에만 검토
- OCO/OTO 조건주문은 일반 주문 원장이 안정화된 뒤 추가
- LLM 판단만으로 자동 승인하지 않음

## 6. 실거래 착수 조건

- 토스증권 Open API 사용 승인이 완료되어야 한다.
- 공식 OpenAPI 스펙을 저장하고 변경 감지 테스트를 운영해야 한다.
- 비밀 저장소, 키 교체, 토큰 만료·폐기 절차가 검증되어야 한다.
- 주문 멱등성, 미확정 응답 조정, 체결 원장 대조 테스트가 통과해야 한다.
- 고액 주문, 거래시간, 가격범위, 주문 가능 금액, 매도 가능 수량 검사가 있어야 한다.
- 모바일 재인증, 승인 만료, 중복 탭 승인 방지가 있어야 한다.
- paper와 shadow 환경에서 정한 기간 동안 중복·유실 주문이 0건이어야 한다.
- 독립적인 보안·위험 검토와 사용자의 최종 실거래 활성화 승인이 있어야 한다.
