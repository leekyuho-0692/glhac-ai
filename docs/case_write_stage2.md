# Case 쓰기 통합 — 2단계

기준: `ce402c2`, `refactor/case-write-service`. 운영 접속·배포·커밋·브랜치 변경 없이 작업.
cli-v2는 Port 1의 API 연결 실패로 REJECT되어 지시된 fallback을 사용했다.

## 전환한 쓰기 지점

| 기존 함수 | 통합 대상 |
|---|---|
| `register` | 가입 프로필·식별 컬럼 초기화 |
| `create_case` | 조직 프로필 상속·식별 컬럼 초기화 |
| `set_case_sector` | `sector`, `sectors` |
| `delete_product` | 합치기·삭제의 `product_aliases` |
| `_apply_owned_autofill` | 업체 식별값·확장 프로필 자동채움 |
| `_revert_failed_autofill` | 실패한 자동채움의 프로필·식별값 회수 |
| `_autofill_products` | 자동 별칭 추가·정리 |
| `_confirm_autofill` | 사람이 선택한 업체·프로필 후보 |
| `apply_pending_extractions` | `pending_handled` — 제외 전용 분기 포함 |
| `update_profile` | 수동 프로필·식별 컬럼 저장 |
| `put_halal_org` | 조직 구성원·자격 확인 |

위 11곳 외에 `renew_case`의 식별 컬럼 초기화도 `new_case`로 통합했다.
`app/case_write_service.py`가 깊은 base 스냅샷, 최신 재조회, 병합과 실제 대입을 담당한다.
서비스는 flush/commit하지 않으며 트랜잭션 안의 앞선 서비스 쓰기는 보존한다.

## 충돌 정책

| 키 | base 이후 양쪽이 수정했을 때 |
|---|---|
| `sector`, `sectors`, `pending_handled`, `product_aliases`, `halal_org`, `email_consent`, `pic_*`, `cp_*` | 최신 우선 |
| 자동·수동 혼합: `city`, `country`, `zip`, `office_phone`, `business_type`, `establishment_date`, `corporate_reg_no`, `company_name_ko/en`, `responsible_person_en`, `total_employee` | 최신 우선 — 사람 결정 별도 저장 전까지 보수적으로 보호 |
| `_registration_extraction` 기계 근거 | 새 값 우선, 근거 객체 전체 교체 |
| 미분류 확장 키·케이스 식별/연락처 컬럼 | 최신 우선 |
| 기존 `DocumentAsset.fields`의 기계 추출값 | 1단계의 새 값 우선 정책 유지 |

한쪽만 변경했으면 그 변경을 채택한다. 누락(삭제)과 `None`을 구분하며,
별칭·할랄조직 등 맵의 서로 다른 하위 키는 함께 보존한다.
자동채움은 분석 전에 복사한 케이스 스냅샷도 전달하며, 그 사이 수정된 값은 확인 대기로 남긴다.

## 잠금과 응답

- `app/db.py::lock_cases`: case_id 정렬 → wf advisory/SQLite writer 잠금 → 케이스 행 잠금. wf 잠금 호출은 이 함수에만 있다.
- `lock_case_set`: 외부 작업 종료 후 필요한 집합을 계산하고 잠금 후 재계산한다. 확대되면 추가 잠금 전에 rollback하고 합집합으로 재시도한다. 총 3회 획득 시도 후에도 확대되면 409.
- 공유 시설 경로는 조직 문서 보호 대상과 조직 시설을 공유하는 다른 조직의 케이스를 처음부터 포함한다. 공장 분류도 최종 commit까지 잠금을 유지한다.
- PostgreSQL의 역순 추가 잠금은 SQL 실행 전에 409로 차단한다. SQLite는 DB 전체 writer 잠금을 쓰므로 기존 여러 케이스 일괄 명령을 유지한다.
- commit/rollback 및 SAVEPOINT rollback에 맞춰 잠금 캐시를 정리한다.
- ZIP `checklist/missing/complete`는 병합 후 저장 fields로 계산한다. 잠금 전 조회한 `zip/factory_zip`은 최종 주소가 같은 경우 재사용하며 외부 조회를 반복하지 않는다.
- 비동기 검증의 시작 스냅샷과 저장 시 최신 추출값(거절 식별자 포함), 문서 유형·본문·파일명이 다르면 검증 결과·진단·회수를 모두 폐기한다.

## 검증

기존 테스트의 기대값은 변경하지 않았다.
새 테스트는 `app/`·`scripts/` 정적 쓰기 검사, WAL 경합, 실제 함수의 잠금 순서 기록,
후보 재생성 시 rollback/재시도, 409 상한, SAVEPOINT, ZIP 응답, 늦은 검증 결과를 다룬다.
PostgreSQL 잠금 순서는 dialect와 잠금 호출을 기록하는 격리 SQLite 테스트로 확인한다.
실제 PostgreSQL 서버에서의 교착 실험은 실행하지 않는다.

- 신규 회귀 + ZIP 안전성/병렬 처리 테스트: **165 passed**.
- Node: 지정된 루프로 **3개 파일 통과, FAIL 출력 0**.
- 무동작 실험: 별도 pytest 프로세스에서 `lock_cases`를 no-op으로 교체하자
  commit/rollback 경합 테스트 **2개 모두 실패**했다. 실패 사유는 첫 트랜잭션 종료 전
  두 번째 writer가 진입했다는 assertion이다. 정상 helper에서는 **2개 모두 통과**했다.
- 지정 명령의 전체 pytest 최종 재실행: **10,065 passed / 1 failed / 22 warnings**, 844.10초.
  실패는 아래 기존 기대값 충돌 1건이다. **실패 0 조건은 미달성**이다.
  로그: `/private/tmp/stage2-pytest-final.log`.

요구사항 충돌: `tests/test_doc_intake_integrity.py:487`은 검증 중 추출값이 바뀐 뒤에도
이전 `_verify.verdict == 'ok'`를 저장하도록 요구한다. 테스트는 `_sources.company_name=manual`인
수동 수정 시나리오다. 새 무효화 규칙에서는 변경 주체와 무관하게 검증 입력값이 달라지면
이전 결과를 폐기하므로 이 assertion과 충돌한다. 기대값 변경 금지 지시에 따라 그대로 두었다.

이 1건에 대한 예외가 허용될 경우 적용할 변경(현재 미적용):

```diff
-        assert fields["_verify"]["verdict"] == "ok"
+        assert "_verify" not in fields
```

바로 다음 줄의 최신 fields 보존 검사는 그대로 유지한다.
오케스트레이터 승인으로 적용함
