# KMA 1360 P1 3차 수정 및 전수 점검

최신 작업은 `/Users/dany/glhac-wt-residual`의 `fix/polish-notify-parse`에서 진행했다.
아래 기존 절의 집계·검증·승인 대기 문구는 당시 기록이며, 현재 결과는 문서 끝의
**검수 2차 반려 후 병합 보정·설명문 재감사** 절을 따른다.

작업 위치: `/Users/dany/glhac-wt-bizparse` · HEAD: `bceba9c` · 미커밋 유지. 이 보고서는 2차 수정 보고서를 대체한다.

## 판정 경로 수정

1. `_kma_name_only_allowed()`를 이름 단독 조건의 단일 함수로 사용한다. note/source/e_number/cert_no에 비어 있지 않은 텍스트가 있거나 docs가 있거나 source_known이 거짓이면 KMA→사전 연결, 일반 alcohol 구분, KMA 확정을 모두 적용하지 않는다. `screen_merged()`뿐 아니라 `screen(material)`, `_match()`, `v1_rule()`, `explain()`, `document_evidence()`의 내부 사전 조회도 같은 제한을 따른다. 부가 문맥이 있는 CAS 입력을 안전한 사전 이름에 우회 연결하지 않으므로, 57-55-6의 ethanol/glycerin 주석은 운영과 같은 medium 키워드 위험·증빙 요구로 최종 판정한다. KMA와 무관하게 직접 매칭되는 기존 사전 판정 정책은 유지한다.
2. 사전 충돌은 매칭한 행 및 그 행과 유효 CAS 하나 이상을 직접 공유하는 모든 행의 `name_id`·`name_en`으로 조회한다. CAS 여러 개를 가진 중간 행을 거쳐 다른 CAS까지 재귀적으로 확장하지 않는다. KMA 입력 이름/CAS 일치 조건은 그대로 유지한다. 색인 제외 이름도 사전 충돌 조회에는 포함하며, 사전의 halal·mushbooh·haram 판정이 모두 KMA 확정보다 우선한다.
3. 사전의 물질 동일성 색인과 KMA 이름 색인은 `app.kma1360.normalize_name()` 하나를 공유한다. NFKD → 결합문자 제거 → 소문자 → 공백·하이픈·괄호 정리 순서다. 숫자·단어·어순은 버리지 않는다. 일반 위험어 탐지에 필요한 기존 `_norm_key()`의 혼동문자·철자 숫자 처리와는 역할이 다르다.
4. 끝 괄호 분리는 영문 이름 후보에만 허용한다. 공백을 제외한 문자 중 영문자 비율 65% 이상과 3글자 이상 연속 영문자를 요구하고, 함량·비율·주석·수식·약어·자격 설명·OCR 의심 표기는 원문 이름에 남긴다. `%`, `approx`, `ca.`, `w/w`, `w/v`, `v/v`, 농도 단위·비율·숫자만 있는 설명을 별칭으로 만들지 않는다. 화학 위치 번호와 명명법 숫자(`2-Ethoxybenzamide`, `Benzophenone-4`, `PEG-25 PABA`, `Basic Yellow 57`)는 함량 수치와 구분해 유지한다.
5. 이름에 `!`, 단어 중간 마침표, 영문자 사이 숫자가 있으면 각각 `ocr_exclamation`, `ocr_intraword_dot`, `ocr_intraword_digit`으로 제외한다. `|` 및 연속 특수문자 제외도 유지한다. `1,2-`, `-2-`, `C12`, `C12-15`는 새 오독 규칙에 걸리지 않는다. JSON 메타데이터가 없거나 잘못 `true`여도 런타임 로더와 입력 해석기가 같은 규칙으로 다시 차단한다.

## 원문·재생성

원문 PDF는 읽기 전용으로 사용했다.

`/private/tmp/claude-501/-Users-dany-IdeaProjects-llmdev/c9e702da-a2c7-4fd5-bf64-d8685f495f89/scratchpad/kma1360/KMA_1360_2021.pdf`

- 원문 SHA256: `fa3562bbd86256a09f91ad18014421fe62914a20cff5055c2b6a714d790f531b` (작업 전후 동일).
- 최종 JSON SHA256: `4786271b66bf2598b285767ae33999582b8c62de418264367e621b1860489e0c`.
- 182쪽의 기존 텍스트 레이어를 읽어 4,112행을 재생성했다. a.1 33 / a.2 8 / a.3 5 / a.4 3 / b 6 / c.1 19 / c.2 4,038행이다. 메모리 재추출 결과와 저장 JSON이 전체 일치한다.
- 기존 no/section/page/cas는 모든 행에서 동일하다. 잘못 분리되던 별칭 13개는 원문 이름으로 합쳐 보존했다. 문자열을 추정 교정하지 않았다.
- 유효 CAS 연결 3,905개, 서로 다른 CAS 3,591개를 모두 보존했다. CAS 체크디지트 실패 14건·형식 실패 70건은 계속 제외한다.
- 이번 이미지 대조는 p.12·43·54·154·176의 전체 페이지를 대상으로 했다. p.154 No.3420의 Approx. 55%/45%가 혼합물 구성비인 것, p.43 No.772와 p.176 No.3909가 CAS 107-41-5를 공유하는 것, 지적된 오독 3건의 정상 인쇄 표기를 확인했다. 4,112행 전체를 이미지와 육안 대조한 것은 아니다.

재생성 명령:

```sh
.venv/bin/python scripts/build_kma1360.py /private/tmp/claude-501/-Users-dany-IdeaProjects-llmdev/c9e702da-a2c7-4fd5-bf64-d8685f495f89/scratchpad/kma1360/KMA_1360_2021.pdf --audit /private/tmp/kma1360-third-pass-baseline/extraction-audit.json
```

## 이름 제외 전수 집계

119쪽, 329행의 이름 329개를 `indexable=false`로 표시했다. 화학물질(c.2) 이름 색인에서 실제로 제외한 것은 324개다. 나머지 5개(a.1 3개, b 2개)는 원래 색인 대상이 아닌 범주 설명이다.

2차 수정의 282개 제외 행 중 281개가 계속 표시되며 48개 행이 새로 제외되었다. No.2646의 `NaaP04`는 이제 독립 `name_en`으로 분리하지 않고 `Natrium Fosfat (NaaP04)` 원문에 남기므로 독립 별칭 제외 건수에서 빠진다. `NaaP04` 자체의 입력 차단 및 이전 JSON 별칭의 런타임 차단은 유지된다. 따라서 순증은 47개(282 → 329)다.

새 느낌표·중간 마침표·영문자 사이 숫자 규칙은 중복 제거 55개 이름에 적용된다. 아래 사유별 수는 중복 집계이며, 확정된 OCR 오류 수가 아니다. 명명법·색소 코드 등 정상 표기도 보수적으로 제외될 수 있다.

| 사유 코드 | 이름 수 |
| --- | --- |
| formula_like | 71 |
| ocr_exclamation | 12 |
| ocr_intraword_closer | 3 |
| ocr_intraword_digit | 24 |
| ocr_intraword_dot | 21 |
| ocr_noise | 31 |
| repeated_special_characters | 131 |
| unbalanced_brackets | 74 |

## 검수 오독 3건

| 위치 | 인쇄 원문 | 이전 JSON 이름 | 현재 처리 |
|---|---|---|---|
| p.12 c.2 No.63 | Bismuth Subcarbonate | Bismuth Sub.carbonate | 영문 별칭 분리 거절. 전체 원문 name_id에 보존하고 ocr_intraword_dot으로 indexable=false |
| p.43 c.2 No.757 | Lilial | Lilia! | ocr_exclamation, indexable=false |
| p.54 c.2 No.1020 | 3-Methyl-1,2-Cyclopentanedione | 3-Methyl-1,2-Cyclopentanedi9ne | ocr_intraword_digit, indexable=false |

세 입력 모두 KMA 매칭·자동등록 불가이며, 원래 유효 CAS는 보존한다. 기존 오독 `NaaP04`, `2-Ethylbutyra).dehyde`, `Hydroxyphenyllf'ropionate` 차단도 유지한다.

## 괄호 패턴 전수 결과

분리 조건을 만족하는 끝 괄호 후보 592개 중 507개는 이름 별칭으로 유지하고 85개는 거절했다. 전체 원문 이름의 모든 단순 괄호를 훑어 함량·비율·주석 패턴 8건(7행)을 발견했다. 아래는 해당 패턴 전수 목록이다. 숫자 위치 표기나 OCR 잡음도 후보에 포함되므로 모두 실제 함량이라는 의미는 아니다. 어떤 경우든 아래 텍스트가 독립 별칭으로 새로 분리되지 않는다.

| 페이지 | 절 | No. | 괄호 원문 |
| --- | --- | --- | --- |
| 16 | c.2 | 143 | Hydrogen Peroxide Solution Topical |
| 17 | c.2 | 160 | Diluted Isosorbide Mononitrate |
| 48 | c.2 | 866 | 2,4-Dimet:1:1ylphenyl |
| 50 | c.2 | 911 |  1: 1: 1 |
| 115 | c.2 | 2485 | 5 / 6 |
| 154 | c.2 | 3420 | Approx. 55% |
| 154 | c.2 | 3420 | Approx. 45% |
| 162 | c.2 | 3566 |  1: 1 |

이번 수정으로 별칭 분리를 취소한 13행은 다음과 같다. 기존 주석·수식·OCR 문자열을 원문 이름에 합쳐 보존했다.

| 페이지 | 절 | No. | 분리 취소한 별칭 |
| --- | --- | --- | --- |
| 9 | c.1 | 10 | AluminumOxide/ Al203 |
| 12 | c.2 | 63 | Bismuth Sub.carbonate |
| 13 | c.2 | 64 | Brompheniram.ine Maleat |
| 14 | c.2 | 100 | Diso~yramyde Phosphate |
| 16 | c.2 | 143 | Hydrogen Peroxide Solution Topical |
| 17 | c.2 | 160 | Diluted Isosorbide Mononitrate |
| 28 | c.2 | 415 | Benzc;&gt;caine |
| 121 | c.2 | 2634 | B-D-Glucopyranuronic Acid, C6H100 |
| 121 | c.2 | 2636 | Hydroxybutanedioic Acid, HOOCCH2CHOHCOOH |
| 121 | c.2 | 2646 | NaaP04 |
| 143 | c.2 | 3175 | Quatemium-1.5 |
| 154 | c.2 | 3420 | Approx. 45% |
| 163 | c.2 | 3599 | 1 h-Indole-2 ,3-Dione |

끝 괄호 분리를 거절한 85개 후보 전체:

| 페이지 | 절 | No. | 거절 후보 | 사유 |
| --- | --- | --- | --- | --- |
| 6 | a.1 | 34 | minyak permen | formula_abbreviation_or_qualification |
| 7 | a.3 | 1 | ketan/ singkong | formula_abbreviation_or_qualification |
| 9 | c.1 | 1 | MgO | formula_abbreviation_or_qualification |
| 9 | c.1 | 2 | ZnO | formula_abbreviation_or_qualification |
| 9 | c.1 | 3 | Ti02 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification, formula_like |
| 9 | c.1 | 10 | AluminumOxide/ Al203 | unqualified_number |
| 9 | c.1 | 17 | Si02 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification, formula_like |
| 9 | c.1 | 18 | SnO | formula_abbreviation_or_qualification |
| 12 | c.2 | 63 | Bismuth Sub.carbonate | ocr_intraword_dot |
| 13 | c.2 | 64 | Brompheniram.ine Maleat | ocr_intraword_dot |
| 14 | c.2 | 100 | Diso~yramyde Phosphate | ocr_noise |
| 16 | c.2 | 143 | Hydrogen Peroxide Solution Topical | concentration_ratio_or_note |
| 17 | c.2 | 160 | Diluted Isosorbide Mononitrate | concentration_ratio_or_note |
| 28 | c.2 | 415 | Benzc;&gt;caine | ocr_noise, repeated_special_characters |
| 34 | c.2 | 559 | Isomer Mixture | formula_abbreviation_or_qualification |
| 35 | c.2 | 571 | Resin | formula_abbreviation_or_qualification |
| 38 | c.2 | 635 | Isomer Unspecified | formula_abbreviation_or_qualification |
| 40 | c.2 | 683 | Mixture Of Isomers | formula_abbreviation_or_qualification |
| 50 | c.2 | 911 | 1: 1: 1 | not_english_substance_name, concentration_ratio_or_note, unqualified_number |
| 61 | c.2 | 1151 | Isomer Unspecified | formula_abbreviation_or_qualification |
| 61 | c.2 | 1152 | Isomer Unspecified | formula_abbreviation_or_qualification |
| 64 | c.2 | 1222 | lalpha,2beta,5alpha | unqualified_number, formula_abbreviation_or_qualification |
| 64 | c.2 | 1229 | Isomer Unspecified | formula_abbreviation_or_qualification |
| 67 | c.2 | 1293 | 5&gt;EO &lt; 20 | not_english_substance_name, unqualified_number, ocr_noise |
| 80 | c.2 | 1573 | En | not_english_substance_name, formula_abbreviation_or_qualification |
| 84 | c.2 | 1680 | Mixed O,M,P | formula_abbreviation_or_qualification |
| 93 | c.2 | 1921 | Free Acid | formula_abbreviation_or_qualification |
| 99 | c.2 | 2067 | Mixed Esters | formula_abbreviation_or_qualification |
| 101 | c.2 | 2136 | Mixed O,M,P | formula_abbreviation_or_qualification |
| 104 | c.2 | 2199 | Cis And Trans Mixture | formula_abbreviation_or_qualification |
| 108 | c.2 | 2324 | Sodium Salt | formula_abbreviation_or_qualification |
| 110 | c.2 | 2374 | Mixture of Isomers | formula_abbreviation_or_qualification |
| 120 | c.2 | 2619 | Endo- | formula_abbreviation_or_qualification |
| 120 | c.2 | 2631 | H2 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification, formula_like |
| 120 | c.2 | 2632 | N2 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification, formula_like |
| 120 | c.2 | 2633 | C6H4(COOH)2 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification, ocr_intraword_digit |
| 121 | c.2 | 2634 | B-D-Glucopyranuronic Acid, C6H100 | unqualified_number, ocr_intraword_digit |
| 121 | c.2 | 2636 | Hydroxybutanedioic Acid, HOOCCH2CHOHCOOH | unqualified_number, ocr_intraword_digit |
| 121 | c.2 | 2637 | CH2(COOH)~ | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification, ocr_noise |
| 121 | c.2 | 2638 | H2C204 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification, ocr_intraword_digit, formula_like |
| 121 | c.2 | 2641 | H2C03 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification, ocr_intraword_digit, formula_like |
| 121 | c.2 | 2642 | HCI04 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification, formula_like |
| 121 | c.2 | 2643 | NH40H | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification, ocr_intraword_digit, formula_like |
| 121 | c.2 | 2644 | Ba(OH)2 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification |
| 121 | c.2 | 2645 | Ca(OH)2 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification |
| 121 | c.2 | 2646 | NaaP04 | unqualified_number, formula_like |
| 121 | c.2 | 2647 | Na2HP04 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification, ocr_intraword_digit, formula_like |
| 121 | c.2 | 2648 | (NH4)3P04 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification |
| 121 | c.2 | 2649 | KsPQ4 | unqualified_number, formula_abbreviation_or_qualification, formula_like |
| 121 | c.2 | 2650 | CaCl2 | unqualified_number, formula_abbreviation_or_qualification, formula_like |
| 121 | c.2 | 2651 | NaCI02 | unqualified_number, formula_abbreviation_or_qualification, formula_like |
| 121 | c.2 | 2652 | NaCI03 | unqualified_number, formula_abbreviation_or_qualification, formula_like |
| 121 | c.2 | 2653 | KN03 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification, formula_like |
| 121 | c.2 | 2654 | NaN03 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification, formula_like |
| 121 | c.2 | 2655 | Al2(S04)3.1sH20 | not_english_substance_name, unqualified_number |
| 121 | c.2 | 2656 | (NH4)2S04 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification |
| 121 | c.2 | 2657 | K2S04 | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification, ocr_intraword_digit, formula_like |
| 121 | c.2 | 2658 | MnSQ4 | unqualified_number, formula_abbreviation_or_qualification, formula_like |
| 122 | c.2 | 2659 | NaAl02 | unqualified_number, formula_abbreviation_or_qualification, formula_like |
| 123 | c.2 | 2686 | TSP | formula_abbreviation_or_qualification |
| 123 | c.2 | 2687 | PAC | formula_abbreviation_or_qualification |
| 123 | c.2 | 2704 | NaBr | formula_abbreviation_or_qualification |
| 125 | c.2 | 2747 | S | not_english_substance_name, formula_abbreviation_or_qualification |
| 128 | c.2 | 2817 | Fe(S04)3.XH20 | not_english_substance_name, unqualified_number |
| 128 | c.2 | 2821 | DMAPA | formula_abbreviation_or_qualification |
| 130 | c.2 | 2874 | Dimer | formula_abbreviation_or_qualification |
| 132 | c.2 | 2918 | TSPP | formula_abbreviation_or_qualification |
| 132 | c.2 | 2920 | HEDP | formula_abbreviation_or_qualification |
| 133 | c.2 | 2942 | PGMAC | formula_abbreviation_or_qualification |
| 133 | c.2 | 2963 | NaOCl | formula_abbreviation_or_qualification |
| 142 | c.2 | 3164 | + | not_english_substance_name |
| 143 | c.2 | 3175 | Quatemium-1.5 | unqualified_number |
| 144 | c.2 | 3198 | + | not_english_substance_name |
| 144 | c.2 | 3205 | + | not_english_substance_name |
| 147 | c.2 | 3258 | 16035 | not_english_substance_name, unqualified_number |
| 154 | c.2 | 3408 | C.I. 47005 | not_english_substance_name, unqualified_number, ocr_intraword_dot |
| 154 | c.2 | 3410 | C.I. 42051 | not_english_substance_name, unqualified_number, ocr_intraword_dot |
| 154 | c.2 | 3418 | Inci | formula_abbreviation_or_qualification |
| 154 | c.2 | 3420 | Approx. 45% | not_english_substance_name, concentration_ratio_or_note, unqualified_number |
| 162 | c.2 | 3559 | 2 | not_english_substance_name, unqualified_number |
| 162 | c.2 | 3566 | 1: 1 | not_english_substance_name, concentration_ratio_or_note, unqualified_number |
| 163 | c.2 | 3595 | AHTN | formula_abbreviation_or_qualification |
| 163 | c.2 | 3599 | 1 h-Indole-2 ,3-Dione | not_english_substance_name, unqualified_number |
| 172 | c.2 | 3810 | Termasuk Poloxamer 182, 338, 183,307,338,407,181,402,dan lain-lain | not_english_substance_name, unqualified_number, formula_abbreviation_or_qualification |
| 179 | c.2 | 3961 | C.I. 77891 | not_english_substance_name, unqualified_number, ocr_intraword_dot |

## 직접 판정 실측

기본 사전은 `app/ontology_data.json`에서 메모리로 적재했다. 위험 사전 두 구성은 명시한 이름에 mushbooh/high를 추가했다. `57-55-6`의 두 note 반례와 Approx. 45%·새 오독 3건은 `git show HEAD:app/screening.py`로 불러온 bceba9c 코드와 반환 객체 전체가 같았다.

| 입력 | note / 사전 구성 | result | status | severity | decision_by | v1_risk |
| --- | --- | --- | --- | --- | --- | --- |
| 57-55-6 | contains ethanol | NEEDS_EVIDENCE | mushbooh | medium | v1_rule | medium |
| 57-55-6 | glycerin carrier | NEEDS_EVIDENCE | mushbooh | medium | v1_rule | medium |
| Approx. 45% | 기본 사전 | NEEDS_EVIDENCE | mushbooh | low | unmatched_default | low |
| Bismuth Sub.carbonate | 기본 사전 | NEEDS_EVIDENCE | mushbooh | low | unmatched_default | low |
| Lilia! | 기본 사전 | NEEDS_EVIDENCE | mushbooh | low | unmatched_default | low |
| 3-Methyl-1,2-Cyclopentanedi9ne | 기본 사전 | NEEDS_EVIDENCE | mushbooh | low | unmatched_default | low |
| 1,2-Hexanediol | 기본 사전 | PASS | halal | low | kma1360 | low |
| Propylene Glycol | 기본 사전 | PASS | halal | low | ontology | low |
| 100-51-6 | 기본 사전 | PASS | halal | low | kma1360 | low |
| Benzyl Alcohol (100-51-6) | 기본 사전 | PASS | halal | low | kma1360 | low |
| 2-Methylpentane-2,4-Diol | Hexylene Glycol = mushbooh/high | NEEDS_EVIDENCE | mushbooh | high | ontology | low |
| 100-51-6 | Bénzyl Alcohol = mushbooh/high | NEEDS_EVIDENCE | mushbooh | high | ontology | low |

## 회귀·검증

- 기존 검수 36개 및 이번 반례 40개, 합계 **76개**의 심의 진입·결정·발급 요청·승인 경로에서 409를 검증한다. 신규 40개는 오독 3건/Approx. 45% 16개, 동일 CAS/유니코드 사전 충돌 이름·CAS 16개, 57-55-6의 note 8개다. note는 실제 Material을 재스크리닝·저장한 뒤 차단을 확인한다. 정상 확정 원료 4종의 네 경로 16개는 200을 유지한다.
- 3차 수정 시작 시점의 기존 assert **6,015개**를 AST 순서 비교했다. 변경·삭제 **0개**다. 이전 제외 수 282와 NaaP04 분리를 고정 기대하던 두 테스트는 `tests/fixtures/kma1360_v2_*.json`의 과거 데이터 호환성 검증으로 분리했다. 현재 추출 결과는 새 assert로 검증하고, 현재 전체 JSON의 제외·별칭·CAS는 `test_kma1360_p1_round3.py`에서 별도로 전수 검사한다. skip/xfail 및 운영 함수를 성공하도록 대체하는 방식은 사용하지 않았다.
- 최종 수정본의 신규 KMA 반례 및 기존 AI R2 판정 회귀 1,038개 통과. 전역 medium 승격으로 기존 Water의 부정 주석 판정이 변하던 중간 회귀 1건은 해당 전역 변경을 제거하여 해결했다. 증빙 충족도 계산의 docs 제한도 포함한다.
- 전체 테스트: **9,331개 통과, 실패 0개**, 경고 22개, 681.31초. 최종 실행 종료코드 0. 검증 시작 시점과 종료 시점의 코드·JSON·테스트 해시가 모두 동일하다. 명령: `GLHAC_DEV=1 GLHAC_NOTIFY_WORKER=0 GLHAC_RATE_LIMIT_PER_MIN=0 .venv/bin/python -m pytest -q tests/`.
- 최종 `git diff --check` 통과. HEAD `bceba9c` 유지, 스테이징 없음, PDF 원문 해시 동일. 기존 미커밋 변경은 보존했다.
- 7포트 파이프라인은 Port 1에서 api.openai.com 연결 실패로 REJECT(trace `8c9f4c96`)했다. 사용자 제공 AGENTS 지침의 cli-v2 실패 시 fallback에 따라 직접 수정했다. 커밋·푸시·배포는 수행하지 않았다.

## 잔여 위험과 범위

- 정규식 제외는 OCR 철자 정확성의 증명이 아니다. 순수 영문자 치환(I/l, rn/m, v/y), 영문자 누락·삽입, 띄어쓰기 오독, 단어 끝 숫자 등은 남을 수 있다. 검수자가 대조한 15쪽 30항목의 최소 13개 OCR 불일치를 이번 작업에서 모두 독립 재검증했다는 뜻도 아니다. 이번 지적 3건은 이미지 대조했고, 나머지는 전수 이미지 대조가 필요하다.
- 괄호의 영문자 비율·주석/수식 규칙은 보수적 휴리스틱이다. 영어·인도네시아어·라틴 학명 또는 생소한 주석을 완벽하게 구분하지 못한다. 정상 복잡 화학명·수식·CI 코드가 제외되는 오탐도 남는다. 추정 철자 교정이나 새로운 별칭 생성은 하지 않았다.
- CAS 체크디지트 통과는 입력 형식 검증이며, OCR이 다른 유효 CAS를 만든 경우까지 검증하지 못한다. 같은 CAS 행 확장은 사전 충돌을 보존하기 위한 것이며 법적·화학적 동등성의 독립 검증이 아니다.
- a/b 절은 여전히 자동 이름 색인 대상이 아니다. 이 변경은 KMA 유래 판정 경로를 제한하며, 기존 독립 사전 판정이나 유래·인증서 정책 전체를 재설계하지 않는다. 정상 이름·CAS 확정과 기존 고위험 차단은 회귀로 확인한다.
- 이미 저장된 과거 PASS 결과에 대한 일괄 재스크리닝·DB 마이그레이션은 수행하지 않았다. 테스트는 실제 재스크리닝·저장 이후 네 경로를 검증한다. 기존 데이터에 새 판단을 적용하려면 별도의 재처리가 필요하다.
- 파일은 요청대로 미커밋이며 신규 모듈·JSON·테스트·자료가 미추적 상태로 남는다. Git 기반 배포가 완성된 상태가 아니고 실제 배포·Docker 빌드도 수행하지 않았다.

## 제외 이름 전체 목록

현재 JSON의 329개 제외 필드를 모두 나열한다. 표의 `&#124;`와 `&#92;`는 각각 수직선과 역슬래시 표시를 위한 이스케이프다. CAS 빈 칸은 유효 CAS가 없다는 뜻이다.

| 페이지 | 절 | No. | 필드 | 원문 이름 | 사유 | 유지한 CAS |
| --- | --- | --- | --- | --- | --- | --- |
| 5 | a.1 | 1 | name_id | Buah segar_ ifresh fruits) | unbalanced_brackets |  |
| 6 | a.1 | 15 | name_id | Kelapa murni (pure coconu~ | unbalanced_brackets, ocr_noise |  |
| 6 | a.1 | 16 | name_id | Air kelapa murni (pure coconut wate,j | unbalanced_brackets |  |
| 8 | b | 5 | name_id | Selulosa ( cellulose) Selulosa diasetat ( cellulose diacetate) Selulosa asetat ( cellulose acetate) Selulosa eter ( cellulose ethe,j Selulosa triasetat ( cellulose triacetate) Carboxy Methyl Cellulose (CMC) Na-CMC | unbalanced_brackets |  |
| 8 | b | 9 | name_id | Polimer sintetik Polietilen (polyethylene/PE) Polistiren (polystyrene/ PS) Poliamin (polyamine) Poliuretan (polyurethane) Polieter (polyethe,j Poliurea (polyurea) Poliakrilat (polyacrilate) dan garamnya Polivinil Pirolidon (polyvinylpyrrolidone) Polivinil Klorida (polyvinyl chloride/ PVC) Polivinil Alkohol (polyvinyl alcohol/PVA) | unbalanced_brackets |  |
| 10 | c.2 | 7 | name_id | Mepiramin Maleat (Mepyramin | unbalanced_brackets | 59-33-6 |
| 11 | c.2 | 33 | name_id | Asam Aset.a.t/ Asam Cuka | ocr_intraword_dot | 64-19-7 |
| 12 | c.2 | 63 | name_id | Bismut Subkarbonat (Bismuth Sub.carbonate) | ocr_intraword_dot | 5892-10-4 |
| 13 | c.2 | 64 | name_id | Bromfeniramin Maleat (Brompheniram.ine Maleat) | ocr_intraword_dot |  |
| 13 | c.2 | 80 | name_id | Deksklorfeniram.in Maleat | ocr_intraword_dot | 2438-32-6 |
| 14 | c.2 | 100 | name_id | Disopiramida Fosfat (Diso~yramyde Phosphate) | ocr_noise | 22059-60-5 |
| 18 | c.2 | 184 | name_id | Klobetasol Propionat (Clo betasol Propionate} | unbalanced_brackets | 25122-46-7 |
| 22 | c.2 | 269 | name_id | Pindolol [Pindolol) | unbalanced_brackets | 13523-86-9 |
| 23 | c.2 | 281 | name_id | Primakuin Fo~fat | ocr_noise | 90-34-6 |
| 24 | c.2 | 296 | name_id | Salbutamol Su1fat | ocr_intraword_digit | 51022-70-9 |
| 24 | c.2 | 298 | name_id | Selenium Sulfida (Selenium Sulfide] | unbalanced_brackets | 7446-34-6 |
| 25 | c.2 | 320 | name_id | Triheksifenidil Hidroklorida (Trihexyphenidyl Hydrochloride)y | ocr_intraword_closer | 52-49-3 |
| 26 | c.2 | 361 | name_id | ((3a,4,5,6,7,7a.:.Hexahydro-4,7- Methano -lh-Inden-5(0r 6)- Yl)Oxy) | repeated_special_characters | 94248-38-1 |
| 27 | c.2 | 376 | name_id | (Nz)-N-( 1-(5-Bicyclo(2.2.1 ]Hept-2-Enyl)Ethylidene ]Hydroxylamine | unbalanced_brackets | 65416-21-9 |
| 28 | c.2 | 415 | name_id | Benzokain (Benzc;&gt;caine) | ocr_noise, repeated_special_characters | 94-09-7 |
| 30 | c.2 | 459 | name_id | 2-Ethenyl-2-Methyl-5-Prop ... 1- En-2-Yloxolane | repeated_special_characters | 13679-86-2 |
| 32 | c.2 | 493 | name_id | (E)-1-(2,6,6-Trimethyl-1- Cyclohexa- 1,3-Dienyl)But-2- En-1-:0ne | repeated_special_characters | 23696-85-7 |
| 33 | c.2 | 513 | name_id | (Z)-3-Hexen-1-Yl 3-Methyl~utanoate | ocr_noise | 35154-45-1 |
| 33 | c.2 | 528 | name_id | (+ /-)-P-Menthan-3-01 | repeated_special_characters | 15356-70-4 |
| 34 | c.2 | 552 | name_id | 1,3-Dioxane, 5-Methyl-2-(2 Methylpropyl) -, Cis- | repeated_special_characters |  |
| 35 | c.2 | 564 | name_id | 1-( 1,2,3,4,6, 7 ,8,8a-Octahydro-2,3,8,8 -Tetramethyl-2- Naphthyl] Ethan-1-0ne | unbalanced_brackets | 68155-67-9 |
| 35 | c.2 | 565 | name_id | 1 '-Acetonaphthone | repeated_special_characters | 941-98-0 |
| 35 | c.2 | 566 | name_id | 8,8-Dimethyl-2,3.,4,5,6, 7- Hexahydro -lH-Naphthalene-2- Carbaldehyde | repeated_special_characters | 68991-97-9 |
| 35 | c.2 | 572 | name_id | 2-Hydroxy-1,2-Diphenyle~anone | ocr_noise | 9000-72-0 |
| 36 | c.2 | 588 | name_id | (+ /-)-Cis-Carveol Acetate | repeated_special_characters | 1205-42-1 |
| 37 | c.2 | 616 | name_id | 2-Butenoic Acid, 2-Methyl-, (3z)-3-Hexen-1-Yl Ester, (2z)- | repeated_special_characters | 84060-80-0 |
| 38 | c.2 | 633 | name_id | 2-(6,6-Dimethyl-4- Bicyclo(3.1.1] Hept-3-Enyl)Ethyl Acetate | unbalanced_brackets | 128-51-8 |
| 38 | c.2 | 653 | name_id | (E)-3,4,5,~,6-Pentamethylhept-3- En-2-One | ocr_noise, repeated_special_characters | 81786-74-5 |
| 39 | c.2 | 660 | name_id | Phenol, 4-Nonyl-, Branched | repeated_special_characters | 84852-15-3 |
| 40 | c.2 | 682 | name_id | Methyl 2-[[(2e}-3, 7- Dimethylocta- 2,6-Dienylidene ]Amino ]Benzoate | unbalanced_brackets | 67801-47-2 |
| 40 | c.2 | 684 | name_id | Benzoic Acid, 2-([2-Methyl-3- [4-(1-Methylethyl)Phenyl]Propylidene )Ami No]-, Methyl Ester | unbalanced_brackets, repeated_special_characters | 91-50-9 |
| 43 | c.2 | 757 | name_id | Lilia! | ocr_exclamation | 80-54-6 |
| 43 | c.2 | 767 | name_id | Lyra! | ocr_exclamation | 31906-04-4 |
| 44 | c.2 | 777 | name_id | Pyridine, 2-(2,~- Dimethylcyclohexyl)- | ocr_noise, repeated_special_characters | 885702-72-7 |
| 44 | c.2 | 795 | name_id | Nerolidol, Cis~(+)- | ocr_noise | 142-50-7 |
| 45 | c.2 | 804 | name_id | 2-[[(ls,4s,6s)-1,7,7-Trimethyl-6- Bicyclo [2.2.1 ]Heptanyl)Oxy)Eth anol | unbalanced_brackets | 7070-15-7 |
| 45 | c.2 | 805 | name_id | 3-Hexene, 1,1',1"-(Ethylidyne Tris(Oxy)) Tris-, (3z,3'z,3"z)- | repeated_special_characters | 215305-10-5 |
| 46 | c.2 | 825 | name_id | Methyl 4..:Methylbenzoate | repeated_special_characters | 99-75-2 |
| 46 | c.2 | 838 | name_id | Hexyl 2-(4-(Diethylamino)-2-Hydroxybenzoyl] Benzoate | unbalanced_brackets | 302776-68-7 |
| 47 | c.2 | 850 | name_id | Silane, Dichlorodimethyl-, Reaction Products with Silica | repeated_special_characters | 68611-44-9 |
| 48 | c.2 | 868 | name_id | 2-(2-(4-Methylcyclohex-3-En-1-Yl)Propyl]Cyclopentan-1-0ne | unbalanced_brackets | 95962-14-4 |
| 48 | c.2 | 883 | name_id | Prop-2-Enyl 2.-(3- Methylbutoxy) Acetate | repeated_special_characters | 67634-00-8 |
| 49 | c.2 | 902 | name_id | Methyl.Abietate | ocr_intraword_dot | 127-25-3 |
| 51 | c.2 | 937 | name_id | {[(2e)-3, 7-Dimethyl-2,6- Octadien-1-Yl]Oxy}Acetald~hyde | ocr_noise | 65405-73-4 |
| 51 | c.2 | 949 | name_id | 4'-Tert-Butylacetophenone. | repeated_special_characters | 943-27-1 |
| 53 | c.2 | 987 | name_id | .r.-Cyclohexaneacetic Acid, 2- Hydroxy-, Lactone | repeated_special_characters | 6051-03-2 |
| 53 | c.2 | 989 | name_id | 4-(2' ,2'-Dimethyl-6' Methylidenecyclohexyl) Butan-2-0ne | repeated_special_characters | 13720-12-2 |
| 53 | c.2 | 1005 | name_id | Propanoic Acid,2-Methyl-,3A,4,5,6, 7, 7 A-Hexahydro-4, 7- Methano- lH-Inden-5-Yl Ester; | repeated_special_characters | 67634-20-2 |
| 54 | c.2 | 1013 | name_id | 5,5'-Dimethyl~2,2'-Diphenyl- 1,2,2' ,4'-Tetrahydro- 4, 4 'methanylylidene-Bis-Pyrazol-3-0ne; | ocr_noise, repeated_special_characters | 4702-90-3 |
| 54 | c.2 | 1016 | name_id | C.I. Solvent Red 122 | ocr_intraword_dot | 12227-55-3 |
| 54 | c.2 | 1020 | name_id | 3-Methyl-1,2-Cyclopentanedi9ne | ocr_intraword_digit | 765-70-8 |
| 55 | c.2 | 1025 | name_id | 2,4-Ditert-Butyl-6-( 1-(3,5-Ditert- Butyl-2-Hydroxyphenyl)Ethyl]Phenol | unbalanced_brackets | 35958-30-6 |
| 55 | c.2 | 1029 | name_id | 2,2'-(Cyclohexylidenebis((2- Methyl- 4, 1-Phenylene)Azo))Bis(4-Cyclohexylphenol) | repeated_special_characters | 6706-82-7 |
| 55 | c.2 | 1030 | name_id | Xanthylium, 3,6-Bis(Diethylamino)-9-(2,4- Disulfophenyl)-, Inner Salt, Sodium Salt | repeated_special_characters |  |
| 55 | c.2 | 1032 | name_id | Propanoic Acid, 2-Methyl-, 2,2-Dimethyl-1-( 1-Methylethyl)-1,3- Propanediyl Ester | repeated_special_characters | 6846-50-0 |
| 56 | c.2 | 1045 | name_id | Methyl (+ /-)-2,2-Dimethyl-6-Methylene-!- Cyclohexanecarboxylate | ocr_exclamation, repeated_special_characters | 81752-87-6 |
| 56 | c.2 | 1055 | name_id | 1-(3 ,3-Dime~ylcyclohexyl)EthylFormate | ocr_noise | 25225-08-5 |
| 56 | c.2 | 1058 | name_id | N-(l,5-Dimethyl-8- Bicyclo(3.2.1] Octanylidene)Hydroxyl Amine | unbalanced_brackets | 75147-23-8 |
| 56 | c.2 | 1059 | name_id | 2,4-Dimethyl-4,4a,5, 9b-Tetrahydroindeno[ 1,2- DJ[ 1,3]Dioxine | unbalanced_brackets | 27606-09-3 |
| 57 | c.2 | 1076 | name_id | 2 ,2', 4, 4' -Tetrahydroxybenzophenone | repeated_special_characters | 131-55-5 |
| 57 | c.2 | 1082 | name_id | (Z)-1-(2 ,6,6-Trimethylcycl~hexen-1-Yl)But- 2-En-1-0ne | ocr_noise | 23726-92-3 |
| 58 | c.2 | 1100 | name_id | 1,3-Benzodioxole-5-Propanol, Alpha-Methyl-, 5-Acetate | repeated_special_characters | 68844-96-2 |
| 59 | c.2 | 1106 | name_id | Ally! 3,5,5-Trimethylhexanoate | ocr_exclamation | 71500-37-3 |
| 59 | c.2 | 1111 | name_id | Carbonic Acid, Ethyl 2,3,6--Trimethylcyclohexyl Ester | repeated_special_characters | 93981-50-1 |
| 59 | c.2 | 1116 | name_id | 4-Heptanol, 2,6-Dimethyl-,Acetate | repeated_special_characters | 10250-45-0 |
| 60 | c.2 | 1141 | name_id | Benzenepropanoic Acid, 3,5- Bis( 1, 1-Dimethylethyl)-4- Hydroxy-, C7-9-Branched Alkyl Esters | repeated_special_characters | 125643-61-0 |
| 60 | c.2 | 1142 | name_id | Methyl 3-(3,5-Di-Tert-Butyl-4- | unbalanced_brackets | 6386-38-5 |
| 60 | c.2 | 1143 | name_id | Hydroxyphenyllf'ropionate | ocr_noise |  |
| 61 | c.2 | 1161 | name_id | Phenol, 2-(2H-Benzotriazol-2- Yl)-6-Dodecyl-4-Methyl-, Branched and Linear | repeated_special_characters | 125304-04-3 |
| 61 | c.2 | 1169 | name_id | Poly(Oxy-1,2-Ethanediyl), .Alpha.-(Isooctylphenyl)-. Omega.- Hydroxy- | repeated_special_characters | 9004-87-9 |
| 61 | c.2 | 1170 | name_id | Poly(Oxy-1,2-Ethanediyl), .Alpha.-(Nonylphenyl)-. Omega.-Hydroxy-, Branched | repeated_special_characters | 68412-54-4 |
| 61 | c.2 | 1171 | name_id | Poly(Oxy-1,2-Ethanediyl), .Alpha.-(Octylphenyl)-.Omega.- Hydroxy-, Branched | repeated_special_characters | 68987-90-6 |
| 62 | c.2 | 1172 | name_id | Poly(Oxy-1,2-Ethanediyl),.Alpha.- [3,5-Dimethyl-1-(2- Methylpropyl)Hexyl]-.Omega.- Hydroxy- | repeated_special_characters | 60828-78-6 |
| 62 | c.2 | 1173 | name_id | Poly(Oxy-1,2-Ethanediyl), .Alpha.-9-Octadecenyl-.Omega. Hydroxy-, (Z)- | repeated_special_characters | 9004-98-2 |
| 62 | c.2 | 1174 | name_id | Poly(Oxy-1,2-Ethanediyl), .Alpha.- Hydro-.Omega.-Hydroxy-, Mono-C12- 14-Alkyl Ethers, Phosphates | repeated_special_characters | 68511-37-5 |
| 62 | c.2 | 1176 | name_id | Poly(Oxy-1,2-Ethanediyl), A-(4- (1,1,3,3-Tetramethylbutyl)Phenyl]-W- Hydroxy- | unbalanced_brackets | 9002-93-1 |
| 62 | c.2 | 1179 | name_id | Poly(Oxy-1,2-Ethanediyl), A-Tridecyl- W-Hydroxy-, Branched | repeated_special_characters | 69011-36-5 |
| 62 | c.2 | 1180 | name_id | Poly(Oxy-1,2-Ethanedyl), Alpha-[Nonylphenyll-Omega-Hydroxy- | unbalanced_brackets | 9016-45-9 |
| 62 | c.2 | 1181 | name_id | Poly[Oxy(Methyl-1,2-Ethanediyl)], .Alpha.-Butyl-.Omega.- Hydroxy- | repeated_special_characters | 9003-13-8 |
| 63 | c.2 | 1188 | name_id | Formic Acid, Reaction Products With Boron Trifluoride And [ 1 s-( I .Alpha., 3a.Beta., 4 .Alpha. ,8a.B eta.) ]-Decahydro- | ocr_intraword_dot, repeated_special_characters | 68855-38-9 |
| 63 | c.2 | 1189 | name_id | Trans-4-Dec~n-1-Al | ocr_noise | 65405-70-1 |
| 63 | c.2 | 1197 | name_id | 1,2-Di(( 1 '-Ethoxy)Ethoxy)Propane | repeated_special_characters | 67715-79-1 |
| 63 | c.2 | 1201 | name_id | (+ /-)-Pulegone | repeated_special_characters | 15932-80-6 |
| 63 | c.2 | 1208 | name_id | Tetrahydro-c-Methyl-z-Iz- Methylpropen-1-Yl)Pyran (Rose Oxide Also) | unbalanced_brackets | 16409-43-1 |
| 65 | c.2 | 1235 | name_id | .Alpha.-Bisabolol | repeated_special_characters | 515-69-5 |
| 65 | c.2 | 1238 | name_id | 1-{3,3-Dimethylbicyclo[2.2.1 ]Hept-2-Yl) Ethane-1-0ne | unbalanced_brackets | 42370-07-0 |
| 65 | c.2 | 1239 | name_id | (+ /-)-4-Ethyloctanal | repeated_special_characters | 58475-04-0 |
| 65 | c.2 | 1245 | name_id | [2,2-Bis[{3, 7-Dimethyloct-6- Enyl)Oxy] Ethyl]Benzene | unbalanced_brackets | 67634-04-2 |
| 66 | c.2 | 1256 | name_id | L- .Alpha.-Pinene | repeated_special_characters | 7785-26-4 |
| 66 | c.2 | 1269 | name_id | Copper, [Dihydrogen 21- Carboxy-14-Ethyl-4,8, 13, 18- Tetramethyl-20-0xo- 9-Vinyl-3- Phorbinepropionato(2-)]-, 21-Methyl Phytyl Ester, (E)- | repeated_special_characters | 15739-09-0 |
| 66 | c.2 | 1277 | name_id | ( + /-) Ethyl 3-Mercaptobutyrate | repeated_special_characters | 156472-94-5 |
| 66 | c.2 | 1278 | name_id | L-.Beta.-Pinene | repeated_special_characters | 18172-67-3 |
| 67 | c.2 | 1293 | name_id | Alcohols, C6-12, Ethoxylated (5&gt;EO &lt; 20) | ocr_noise | 68439-45-2 |
| 67 | c.2 | 1294 | name_id | Alcohols, C8-10-Iso-, C9-Rich | repeated_special_characters | 68526-84-1 |
| 68 | c.2 | 1322 | name_id | (2.Alpha.,4a.Alpha.,8.Beta.)- Hexahydro-1,1,5,5-Tetramethyl- 2H- 2,4A-Methanonaphthalene-8{5H)-One | unbalanced_brackets, ocr_intraword_dot, repeated_special_characters | 29461-13-0 |
| 69 | c.2 | 1334 | name_id | 1-0xaspiro[4.5]Decan-2-0ne, 8- Methyl-, Trans- | repeated_special_characters | 91069-40-8 |
| 69 | c.2 | 1335 | name_id | 1-0xaspiro[4.S]Decan-6-0l, 2,6, 10, 10-Tetramethyl-, Acetate | repeated_special_characters | 72541-09-4 |
| 69 | c.2 | 1336 | name_id | 1-Spiro[4.5)Dec-7-En-7-Yl-4-Penten-1-One | unbalanced_brackets | 224031-70-3 |
| 69 | c.2 | 1342 | name_id | 2-(3-Heptyl}- l ,3-Dioxolane | unbalanced_brackets | 4359-47-1 |
| 70 | c.2 | 1347 | name_id | 2,4,6-Cycloheptatrien-l-One, 2-Hydroxy-4-( 1-Methylethyl)-,Sodium Salt | repeated_special_characters | 17387-01-8 |
| 71 | c.2 | 1383 | name_id | 2-Pyndinecar~oxylic Acid, Ethyl Ester | ocr_noise | 2524-52-9 |
| 72 | c.2 | 1399 | name_id | 4-Methyl-1-0~aspiro[5.5]Undecene | ocr_noise | 68228-06-8 |
| 72 | c.2 | 1405 | name_id | Benzoic Acid, 2-[[3-(4- Methoxyphenyl)-2-. Methylpropylidenejamino]-, Methyl Ester | unbalanced_brackets, repeated_special_characters | 111753-62-9 |
| 72 | c.2 | 1407 | name_id | Diethyl 2,2'-Thiodiacetate | repeated_special_characters | 925-47-3 |
| 72 | c.2 | 1409 | name_id | 4,9, 12, 12-Tetramethyl-5-0xatricyclo (8.2.0.04,6] Dodecane | unbalanced_brackets | 1209-61-6 |
| 72 | c.2 | 1410 | name_id | Hexanoic Acid, 6-(Acetyloxy)-, Ethyl Ester | repeated_special_characters | 104986-28-9 |
| 72 | c.2 | 1412 | name_id | Hydrocarbons, C4, 1,3- Butadiene- Free, Polymd., Triisobutylene Fraction, Hydrogenated | repeated_special_characters | 93685-81-5 |
| 73 | c.2 | 1421 | name_id | 1, 1 '-(2-Phenylethylidene)Bis( lh-Indole) | repeated_special_characters | 67801-37-0 |
| 73 | c.2 | 1422 | name_id | l-Propanaminium, N,N,N- Trimethyl- 3-[(1-0xo-2- Propenyl)Amino]-, Chloride, Polymer With 2-Propenamide | repeated_special_characters | 75150-29-7 |
| 73 | c.2 | 1424 | name_id | Benzoic Acid, 2-[(1- Oxopropyl)Amino]-, Methyl Ester | repeated_special_characters | 25628-84-6 |
| 73 | c.2 | 1425 | name_id | Cyclohexanol, 4-(1- Methylethyl)-, Acetate, Trans- | repeated_special_characters | 15876-32-1 |
| 73 | c.2 | 1427 | name_id | Ethanone, 1-(4,11,11,-Trimethyl- 8-Methylenebicyclo[7 .2.0]Undec-4-Enyl)-[ lr -(1A,4E,9B] | unbalanced_brackets, repeated_special_characters | 70801-04-6 |
| 74 | c.2 | 1444 | name_id | 2-[2-[2-[Bis[2-(2-Hydroxyethoxy) Propyl]Amino] Ethyl- [2-(2-Hydroxye Thoxy) Propyl]Amino)-1- Methyl-Ethoxy]Ethanol | unbalanced_brackets | 11111-34-5 |
| 75 | c.2 | 1452 | name_id | Methyl 2-([3-( 1,3-Benzodioxol- 5-Yl)- 2-Methylpropylidene ]Amino ]Benzoate | unbalanced_brackets | 111753-60-7 |
| 75 | c.2 | 1455 | name_id | Methyl 3,3- Dimethylbicyclo[2.2.1) Heptane -2-Carboxylate | unbalanced_brackets | 52557-97-8 |
| 76 | c.2 | 1477 | name_id | 2-Methoxy-4-'(Tetrahydro-4- Methylene-2h-Pyran-2-Yl)Phenol | repeated_special_characters | 128489-04-3 |
| 76 | c.2 | 1485 | name_id | l-(l,2,8,8-Te'f:!a.II1ethyl-l,3,4,5,6,7-Hexahydronaphthalen-2- Yl)Ethanone | ocr_noise, ocr_exclamation, ocr_intraword_dot, ocr_intraword_digit, repeated_special_characters | 185429-83-8 |
| 76 | c.2 | 1487 | name_id | Methyl 2-((2,4-Dimethylcyclohex-3- En-1-Yl)Methylideneamino ]Benzoate | unbalanced_brackets | 68845-02-3 |
| 77 | c.2 | 1493 | name_id | (2e,6e)-3, 7.:.oimethylnona-2,6-Dienal | repeated_special_characters | 41448-29-7 |
| 77 | c.2 | 1509 | name_id | 1,3,7-0ctatriene, 3,7-Dimethyl-; 3,7-Dimethylocta-1,3, 7 -Triene; | repeated_special_characters | 3338-55-4, 502-99-8 |
| 80 | c.2 | 1579 | name_id | 2,4~Nonadien-1-0l | ocr_noise | 62488-56-6 |
| 82 | c.2 | 1639 | name_id | 2,2'-(0xybis(Methylene))Difuran | repeated_special_characters | 4437-22-3 |
| 83 | c.2 | 1651 | name_id | 2-Ethylbutyra).dehyde | unbalanced_brackets, ocr_intraword_closer | 97-96-1 |
| 84 | c.2 | 1692 | name_id | 2-Formyl-6,6- Dimethylbicyclo(3.1.1] Hept-2-Ene (Myrtenal) | unbalanced_brackets | 564-94-3 |
| 85 | c.2 | 1706 | name_id | 2-Trans-4-Cis·-7-Cis-Tridecatrienal | repeated_special_characters | 13552-96-0 |
| 86 | c.2 | 1739 | name_id | [ 1, 1 '-Biphenyl)-3,3'- Dicarboxaldehyde, 6,6'- Dihydroxy-5,5'-Dimethoxy- | unbalanced_brackets, repeated_special_characters | 2092-49-1 |
| 90 | c.2 | 1845 | name_id | 2-Methylhepta.n-3-0ne | ocr_intraword_dot | 13019-20-0 |
| 90 | c.2 | 1848 | name_id | 4-Mercapto-4-Methyl- 2-Penta.none | ocr_intraword_dot | 19872-52-7 |
| 93 | c.2 | 1917 | name_id | 4-Azanylbutanoic Acid (4-Aminobutyric acid-15N | unbalanced_brackets | 58485-43-1 |
| 94 | c.2 | 1932 | name_id | Sodium 4-Methyl-2·-0xovalerate | repeated_special_characters | 4502-00-5 |
| 94 | c.2 | 1933 | name_id | Potassium 2-(1'-Ethoxy) Ethoxypropanoate | repeated_special_characters | 100743-68-8 |
| 95 | c.2 | 1955 | name_id | lsoamyl Isobutyrate {lsopentyl Isobutyrate) | unbalanced_brackets | 2050-01-3 |
| 95 | c.2 | 1967 | name_id | 3,7-Dimethyl-2,6-0ctadien-1-Yl 2-Ethylbutanoa~e(Geranyl 2-Ethylbutyrate) | ocr_noise | 73019-14-4 |
| 104 | c.2 | 2210 | name_id | (E)-2-0cten-1 YI Butyrate "'I | repeated_special_characters | 84642-60-4 |
| 109 | c.2 | 2336 | name_id | Phenethyl Mercapta.n | ocr_intraword_dot | 4410-99-5 |
| 109 | c.2 | 2345 | name_id | 2 ,3-Buta.nedithiol | ocr_intraword_dot | 4532-64-3 |
| 109 | c.2 | 2349 | name_id | 4-Methoxy-2-Methyl-2-Buta.nethiol | ocr_intraword_dot | 94087-83-9 |
| 110 | c.2 | 2354 | name_id | Ethane-!, 1-Dithiol | ocr_exclamation, repeated_special_characters | 26914-40-9 |
| 110 | c.2 | 2375 | name_id | Ethyl2-Hydroxyethy1Sulfide | ocr_intraword_digit | 110-77-0 |
| 111 | c.2 | 2382 | name_id | Ally! Thiopropionate | ocr_exclamation | 41820-22-8 |
| 111 | c.2 | 2387 | name_id | (+ /-)-Isobutyl 3- Methylthiobutyrate | repeated_special_characters | 127931-21-9 |
| 111 | c.2 | 2402 | name_id | Ally! Methyl Disulfide | ocr_exclamation | 2179-58-0 |
| 113 | c.2 | 2456 | name_id | N-(Heptan-4- Yl)Benzo[DJ[ 1,3]Dioxole-5-Carboxamide | unbalanced_brackets | 745047-51-2 |
| 115 | c.2 | 2485 | name_id | 2-Methoxy-3(5 / 6)-Ethyl Pyrazine) Methoxypyrazine | unbalanced_brackets | 25680-58-4 |
| 117 | c.2 | 2532 | name_id | 2 ,2'-(Thiodimethylene)Difuran | repeated_special_characters | 13678-67-6 |
| 117 | c.2 | 2533 | name_id | 2,2'-(Dithiodimethylene) Difuran | repeated_special_characters | 4437-20-1 |
| 117 | c.2 | 2534 | name_id | 2-Methyl-3-,5-or 6-(Furfurylthio) Pyrazine | repeated_special_characters | 65530-53-2 |
| 118 | c.2 | 2572 | name_id | 2-Ethyl-4 ,5-Dimethyloxazo~e | ocr_noise | 53833-30-0 |
| 119 | c.2 | 2602 | name_id | (+ /-)-3,5-Diethyl-1,2,4-Trithiolane | repeated_special_characters | 54644-28-9 |
| 119 | c.2 | 2606 | name_id | Nero! | ocr_exclamation | 106-25-2 |
| 120 | c.2 | 2621 | name_id | (+ /-)-Dihydrofarnesol | repeated_special_characters | 37519-97-4, 51411-24-6 |
| 120 | c.2 | 2633 | name_id | Phthalic Acid (C6H4(COOH)2) | ocr_intraword_digit | 100-21-0 |
| 121 | c.2 | 2634 | name_id | Glucoronic Acid (B-D-Glucopyranuronic Acid, C6H100) | ocr_intraword_digit | 21085-72-3 |
| 121 | c.2 | 2635 | name_id | Glutaric Acid '(C3H6(COOH)2) | ocr_intraword_digit | 110-94-1 |
| 121 | c.2 | 2636 | name_id | (DL) Malic Acid (Hydroxybutanedioic Acid, HOOCCH2CHOHCOOH) | ocr_intraword_digit | 6915-15-7 |
| 121 | c.2 | 2637 | name_id | Malanie Acid (CH2(COOH)~) | ocr_noise | 141-82-2 |
| 121 | c.2 | 2638 | name_id | Oxalic Acid (H2C204) | ocr_intraword_digit | 144-62-7 |
| 121 | c.2 | 2641 | name_id | Asam Karbonat (H2C03) | ocr_intraword_digit | 463-79-6 |
| 121 | c.2 | 2643 | name_id | Ammonium Hidroxide (NH40H) | ocr_intraword_digit | 1336-21-6 |
| 121 | c.2 | 2647 | name_id | Natrium Hidrogen Fosfat (Na2HP04) | ocr_intraword_digit | 7558-79-4 |
| 121 | c.2 | 2657 | name_id | Kalium Sulfat (K2S04) | ocr_intraword_digit | 7778-80-5 |
| 123 | c.2 | 2691 | name_id | 2,5-Dimethyl-~-Methoxy-3(2H)Furanone | ocr_noise, repeated_special_characters | 4077-47-8 |
| 124 | c.2 | 2713 | name_id | 00121425 Borneo! | ocr_exclamation | 464-45-9 |
| 124 | c.2 | 2724 | name_id | Ethyl 2,4-Dim~thyl-1,3- Dioxolane-2-Acetate | ocr_noise | 6290-17-1 |
| 127 | c.2 | 2805 | name_id | 3'-Hydroxyacetophenone | repeated_special_characters | 121-71-1 |
| 127 | c.2 | 2807 | name_id | Ally! Hexanoate | ocr_exclamation | 123-68-2 |
| 130 | c.2 | 2870 | name_id | 6-Methyl.:.5-Hepten-2-0ne | repeated_special_characters | 110-93-0 |
| 130 | c.2 | 2886 | name_id | 2-N aphthalenecarboxamide, N,N- 1,4-Pheriylenebis[4-(2- (2,5-Dichlorophenyl)Diazenyl)- | unbalanced_brackets | 3905-19-9 |
| 131 | c.2 | 2910 | name_id | Azobis(Cyclohexane-1-Carbonitrile | unbalanced_brackets | 1855-63-6 |
| 132 | c.2 | 2920 | name_id | !-Hydroxy Ethylidene-1, 1- Diphosphonic Acid (HEDP) | ocr_exclamation, repeated_special_characters | 2809-21-4 |
| 132 | c.2 | 2923 | name_id | 2-(6-Methyl-8-( l-Methylethyljfsicyclo (2.2.2) Oct- 5-En-2(3)-Yl-1,3-Dioxolane | unbalanced_brackets | 68901-32-6 |
| 134 | c.2 | 2981 | name_id | Benzoic Acid,2-[-4- (Diethylamino)-2-Hydroxybenzoyl]-, Hexylester | repeated_special_characters | 302776-68-7 |
| 134 | c.2 | 2982 | name_id | Benzoic Acid, 4,4-((6-(((1, 1-Dimethylethyl}Amino)Carbonyl) Pheny L)Amino)-1,3,5- Triazine- 2,4 Diyl)Diimino) Bis-,Bis-(2- Ethylhexyl) Ester) (Diethylhexyl Butamido Triazone) | unbalanced_brackets, repeated_special_characters | 154702-15-5 |
| 135 | c.2 | 2986 | name_id | Ethyl 4-[Bis(2- Hydroxyethyl) AminolBenzoate | unbalanced_brackets | 15716-30-0 |
| 135 | c.2 | 2992 | name_id | 2,2'-Methylene~Bis-6-(2h- Benzotriazol-2yl)-4- (Tetramethyl- Butyl)-1, 1,3,3-Phenol | ocr_noise, repeated_special_characters | 103597-45-1 |
| 135 | c.2 | 2993 | name_id | 3-(4'-Methylbenzylidene)-Dl- Camphor | repeated_special_characters | 36861-47-9 |
| 136 | c.2 | 3005 | name_id | 2 , 4 6-Trianilino-(P-Carbo-2'-' Ethylhexyl-1 '-0xy)-1,3,5- Triazine | repeated_special_characters | 88122-99-0 |
| 136 | c.2 | 3006 | name_id | {l,3,5)-Triazine-2,4-Bis((4-(2-Ethyl- Hexyloxy)-2-Hydroxy)- Phenyl) -6-(4- Methoxyphenyl) (Bis-Ethylhexyloxyphenol/Methoxyp henyl Triazine) | unbalanced_brackets | 187393-00-6 |
| 136 | c.2 | 3007 | name_id | 1,3,5-Triazine, 2,4,6-Tris [1,1'-Biphenyl)-4-Yl-, (Tris-Biphenyl Triazine) | unbalanced_brackets, repeated_special_characters | 31274-51-8 |
| 140 | c.2 | 3102 | name_id | 3-Acetyl-6-Methylpyran-2,4 (3H)- Dione (D.ehydroacetic Acid) dan garamnya | ocr_intraword_dot | 520-45-6 |
| 140 | c.2 | 3119 | name_id | 3,3'-Bis(l-Hydroxymethyl-2,5-Dioxoimidazolidin-4-Yl)-1, 1 ' -Methylenediurea | repeated_special_characters | 39236-46-9 |
| 141 | c.2 | 3139 | name_id | 2-Methylisothiazol-3(2H)-One Chloro-M-Cresol) | unbalanced_brackets | 2682-20-4 |
| 142 | c.2 | 3146 | name_id | 6,6-Dibromo-4,4-Dichloro-2,2'-Methylene-Diphenol | repeated_special_characters | 15435-29-7 |
| 142 | c.2 | 3147 | name_id | 3,3'-Dibromo-4,4'- Hexamethylene-Dioxydibenzamidine | repeated_special_characters | 93856-83-8 |
| 142 | c.2 | 3148 | name_id | 3,3'-Dibromo-4,4'- Hexamethylene-Dioxydibenzamidine lsethionate | repeated_special_characters | 93856-83-8 |
| 142 | c.2 | 3163 | name_id | N-(Hydroxymethyl)-N- (Dihydroxymethyl-1,3-Dioxo- 2,5- Imidazolidinyl-4)-N'-(Hydroxymethyl) Urea | repeated_special_characters | 78491-02-8 |
| 145 | c.2 | 3215 | name_id | 1,4-Bis-(2",3"-Dihydroxypropyl)- Amino-2- Nitro-5-Chlorobenzene(HC Red No 10) | repeated_special_characters | 95576-89-9 |
| 145 | c.2 | 3216 | name_id | 1,4-Bis-(2",3"-Dihydroxypropyl)- Amino-2- Nitro-5-Chlorobenzene | repeated_special_characters | 95576-92-4 |
| 145 | c.2 | 3218 | name_id | 2,2''-[(4-Amino-3-Nitrophenyl)-lmino]Bisethanol {HC Red No 13) | unbalanced_brackets, repeated_special_characters | 29705-39-3 |
| 145 | c.2 | 3219 | name_id | Garam Hidroklorida Dari 2,2"- [(4-Amino-3-Nitrophenyl)- Imino]Bisethanol | repeated_special_characters | 94158-13-1 |
| 145 | c.2 | 3223 | name_id | 1-(4 "-Aminophenylazo)-2-Methyl-4- (Bis-2- Hydroxyethyl) Aminobenzene HC Yellow No 7) | unbalanced_brackets, repeated_special_characters | 104226-21-3 |
| 145 | c.2 | 3224 | name_id | 2,2'-[(4aminophenyl))-Imino]Bis (Ethanol) -Sulphate N,N- Bis(2-Hydroxyethyl) -P-Phenylenediamine Sulfate | unbalanced_brackets, repeated_special_characters | 54381-16-7 |
| 145 | c.2 | 3231 | name_id | Benzenaminium, 3-[(4,5-Dihydro- 3- Methvl-5-0xo-1- Phenvl-Lh-Pyrazol- 4-Yl)Azo]- N,N,Ntrimethyl-, Chloride | repeated_special_characters | 68391-31-1 |
| 146 | c.2 | 3233 | name_id | Benzenemethanaminium,N- Ethyl-N- [4-[[4-[Ethyl-[(3-Sulfo- Phenyl)- Methyl]-Amino]- Phenyl] [2- Sulfophenyl)-Methylene]-2,5- Cyclohexadien- 1-Ylidene ]-3-Sulfo, Inner Salt, Garam Disodium (Acid Blue 9) | unbalanced_brackets | 3844-45-9 |
| 146 | c.2 | 3234 | name_id | Benzenemethanaminium,N- Ethyl-N- [4-[[4-(Ethyl-[(3-Sulfo- Phenyl)- Methyl]-Amino]- Phenyl] (2- Sulfophenyl)-Methylene]-2,5- Cyclohexadien- 1-Ylidene)-3-Sulfo, Ammonium | unbalanced_brackets | 2650-18-2 |
| 146 | c.2 | 3235 | name_id | Benzenemethanaminium,N- Ethyl-N- [4-[[4-[Ethyl-[(3-Sulfo- Phenyl)- Methyl]-Amino]- Phenyl] [2- Sulfophenyl)-Methylene]-2,5- Cyclohexadien- 1-Ylidene]-3-Sulfo, Aluminium (Acid Blue 9 Aluminum Lake) | unbalanced_brackets | 68921-42-6 |
| 147 | c.2 | 3259 | name_id | Disperse Black 9 Aminophenyl)Azo] Phenyl]Imino]Bis- (Disperse Black 9) | unbalanced_brackets | 20721-50-0 |
| 147 | c.2 | 3264 | name_id | H202 | formula_like | 7722-84-1 |
| 148 | c.2 | 3270 | name_id | 2 ,2' -[[ 4-[ (2-Hydroxyethyl) Amino )-3-Nitrophenyl]IIIlino)Bisethanol (HC Blue No 2) | unbalanced_brackets | 33229-34-4 |
| 148 | c.2 | 3272 | name_id | Ethanol, 2-(4-[Ethy1[(2-Hydroxyethyl) Amino)-2- Nitrophenyljamino]-, dan garam Hydrochloride-nya | unbalanced_brackets, repeated_special_characters | 104516-93-0 |
| 149 | c.2 | 3290 | name_id | 1-((2'-Methoxyethyl) Amino]-2- Nitro- 4-[Di-(2' -Hydro- Xyethyl)Amino] Benzene (HC Blue No 11) | unbalanced_brackets, repeated_special_characters | 23920-15-2 |
| 150 | c.2 | 3311 | name_id | 2-((2-Nitrophenyl) Amino] Ethanol (HC Yellow No 2) | unbalanced_brackets | 4926-55-0 |
| 150 | c.2 | 3318 | name_id | Phenol, 2-Chloro-6- (Ethylamino)-4-Nitro-5) | unbalanced_brackets | 131657-78-8 |
| 151 | c.2 | 3331 | name_id | 4,4'-[l,3-Propanediylbis(Oxy)]-Bisbenzene-1,3-Diamine dan garam Tetrahydrochloridenya | repeated_special_characters | 81892-72-0 |
| 151 | c.2 | 3334 | name_id | 1-Propanol, 3-[[ 4-[Bis(2-Hydro-Xyethyl) Aminoj-z-Nitropheny-L] Amino] HC | unbalanced_brackets | 104226-19-9 |
| 152 | c.2 | 3368 | name_id | Pigment Red 4~, garam Disodium | ocr_noise, repeated_special_characters | 3564-21-4 |
| 154 | c.2 | 3408 | name_id | E104 Quinoline Yellow (C.I. 47005) | ocr_intraword_dot |  |
| 154 | c.2 | 3410 | name_id | E131 Patent Blue V (C.I. 42051) | ocr_intraword_dot |  |
| 154 | c.2 | 3413 | name_id | El50b Caustic Sulphite Caramel | ocr_intraword_digit |  |
| 154 | c.2 | 3414 | name_id | E150c Ammonia Caramel | ocr_intraword_digit |  |
| 154 | c.2 | 3415 | name_id | E150d Sulphite Ammonia Caramel | ocr_intraword_digit |  |
| 156 | c.2 | 3463 | name_id | 4-Methyl-8-M.ethylene Tricyclo(3.3.1.1 3. 7)Decan-2-0l | ocr_intraword_dot | 122760-84-3 |
| 156 | c.2 | 3465 | name_id | 13-0xa Bicyclo{l0.1.0)Trideca- 4,8-Diene, Trimethyl- | unbalanced_brackets | 71735-79-0 |
| 157 | c.2 | 3470 | name_id | Pentanoic Acid, 2-Methyl-, 2-Methylpentyl Ester&#92; | ocr_noise, repeated_special_characters | 90397-38-9 |
| 157 | c.2 | 3478 | name_id | Cyclohexanol, 4-( 1- Methylethyl)-, Propanoate | repeated_special_characters | 63449-95-6 |
| 157 | c.2 | 3479 | name_id | 4-(1(4-,5 Or 6)-Methylbicyclo(2.2.1) Hept-5-En- 2-Yl)-Pyridine | repeated_special_characters | 862079-91-2 |
| 157 | c.2 | 3481 | name_id | 2,38- Methano-3BH-Cyclopenta(l,3) Cyclopropa (1,2) Benzene-4-Methanol, Octahydro-7, 7 ,8,8-Tetramethyl-, Acetate | repeated_special_characters | 59056-62-1 |
| 157 | c.2 | 3484 | name_id | SH-Cyclopenta(H)Quinazoline, 6,7,8,9-Tetrahydro-7,7,8,9,9-Penta.methyl- | ocr_intraword_dot | 1315251-11-6 |
| 158 | c.2 | 3489 | name_id | Spiro[Furan-2(3H),5'-(4, 7- Methano-5H-Indene], Decahydro | unbalanced_brackets, repeated_special_characters | 68480-11-5 |
| 158 | c.2 | 3490 | name_id | Heptanal, 2-((4-Methylphenyl)Methylene]- | unbalanced_brackets | 84697-09-6 |
| 158 | c.2 | 3491 | name_id | Cyclohexanone, 4-((3,3- Dimethylbicyclo [2.2.1 ]Hept-2-Yl)Methyl]-2-Methyl- | unbalanced_brackets | 68901-22-4 |
| 158 | c.2 | 3493 | name_id | l-Propanol, 2-Methyl-3-(( 1, 7, 7-Trimethylbicyclo[2.2. l]Hept-2- Yl)Oxy]- | unbalanced_brackets | 128119-70-0 |
| 158 | c.2 | 3496 | name_id | 2H-Pyran, Tetrahydro-4-Methyl- 2-Phenyl-, (2R,4S)- | repeated_special_characters | 149713-23-5 |
| 158 | c.2 | 3497 | name_id | 4, 7 -Methano-3AH-Indene-3A- Carboxylic Acid, Octahydro-,Ethyl Ester | repeated_special_characters | 129520-41-8 |
| 158 | c.2 | 3498 | name_id | 3- Cyclohexene-1-Methanol, 3,5-Dimethyl-, Acetate | repeated_special_characters | 67634-25-7 |
| 159 | c.2 | 3504 | name_id | 2- Octenoic Acid, 4-Ethyl-, (2E)- | repeated_special_characters | 60308-76-1 |
| 159 | c.2 | 3515 | name_id | 1-Ethyl-3- Methoxytricyclo (2.2.1.02,6]Heptane | unbalanced_brackets | 31996-78-8 |
| 159 | c.2 | 3516 | name_id | Bicyclo(2.2. l)Heptan-2-0l, 1,2,3,3-Tetramethyl-, Endo | repeated_special_characters | 28462-85-3 |
| 159 | c.2 | 3518 | name_id | 7- Octen-1-01, 3,7-Dimethyl-,(S)- | repeated_special_characters | 6812-78-8 |
| 159 | c.2 | 3520 | name_id | Azulene, 1,2,3,4,5,6, 7,8-0ctahydro-1,4-Dimethyl- 7 -( 1- Methylethylidene)-, ( 1 S-Cis)- | repeated_special_characters | 88-84-6 |
| 160 | c.2 | 3528 | name_id | Benzoic Acid, (2,4-Dihydro'o/-3- Methyl)-, Methyl Ester | ocr_noise, repeated_special_characters | 33662-58-7 |
| 160 | c.2 | 3532 | name_id | 3- Decen-5-0ne, 4-Methyl-, (3E)- | repeated_special_characters | 811412-48-3 |
| 160 | c.2 | 3533 | name_id | 2- Propenoic Acid, 2-Methyl-, Polymer With Ethyl 2-Propenoate | repeated_special_characters | 25212-88-8 |
| 161 | c.2 | 3544 | name_id | Cyclohexane, 1-( 1, 1- Dimethylpropyl)-4-Ethoxy-, Cis- | repeated_special_characters | 181258-87-7 |
| 161 | c.2 | 3545 | name_id | Spiro(Bicyclo(4. l .O)Heptane- 2,5'-(l,3)Dioxane), 2',2',3,7,7-Pentamethyl-, ( lalpha,3alpha,6alpha)- | repeated_special_characters | 121251-67-0 |
| 161 | c.2 | 3549 | name_id | 1-Propanol, 2-( 1-(3,3-Dimethylcyclohexyl) Ethoxy]-2- Methyl-, 1-Propanoate | unbalanced_brackets, repeated_special_characters |  |
| 161 | c.2 | 3555 | name_id | Benzenemethanol, A-Methylene-, Acetate | repeated_special_characters | 2206-94-2 |
| 164 | c.2 | 3610 | name_id | Disperse Blue 377 Is A Mixture Of Three Dyes: ( 1) 1,4-Bis[(2-Hydroxyethyl)Amino) Anthra- 9,10-Quinone/(2) 1-[(2-Hydroxyethyl)Amino)-4-[(3-Hydroxypropyl)Amino)Anthra- 9, 10-Quinone/(3) 1~4-Bis[(3-Hydroxypropyl) Amino]Anthra- 9,10-Quinone | unbalanced_brackets, ocr_noise | 4471-41-4, 67674-26-4, 67701-36-4 |
| 164 | c.2 | 3621 | name_id | 2,2'-Methylenebis-4- Aminophenol HCl | repeated_special_characters | 63969-46-0 |
| 165 | c.2 | 3638 | name_id | N ,N'-Bis(2-Hydroxyethyl)-2- Nitro-P-Phenylenediainine | repeated_special_characters | 84041-77-0 |
| 165 | c.2 | 3647 | name_id | CI12480 | formula_like | 6410-40-8 |
| 166 | c.2 | 3661 | name_id | CI15980 | formula_like | 2347-72-0 |
| 166 | c.2 | 3676 | name_id | CI20040 | formula_like | 5979-28-2 |
| 166 | c.2 | 3677 | name_id | CI20470 | formula_like | 1064-48-8 |
| 166 | c.2 | 3679 | name_id | CI24790 | formula_like | 13421-53-9 |
| 166 | c.2 | 3680 | name_id | CI28440 | formula_like | 2519-30-4 |
| 167 | c.2 | 3681 | name_id | CI40215 | formula_like | 1325-54-8 |
| 167 | c.2 | 3682 | name_id | CI42045 | formula_like | 21016-37-5 |
| 167 | c.2 | 3683 | name_id | CI42080 | formula_like | 3486-30-4 |
| 167 | c.2 | 3684 | name_id | CI42090 | formula_like | 2650-18-2 |
| 167 | c.2 | 3685 | name_id | CI42100 | formula_like | 4857-81-2 |
| 167 | c.2 | 3686 | name_id | CI42170 | formula_like | 5863-51-4 |
| 167 | c.2 | 3687 | name_id | CI42510 | formula_like | 632-99-5 |
| 167 | c.2 | 3688 | name_id | CI42520 | formula_like | 3248-91-7 |
| 167 | c.2 | 3689 | name_id | CI42735 | formula_like | 6505-30-2 |
| 167 | c.2 | 3690 | name_id | CI44090 | formula_like | 3087-16-9 |
| 167 | c.2 | 3691 | name_id | CI45100 | formula_like | 3520-42-1 |
| 167 | c.2 | 3692 | name_id | CI45220 | formula_like | 5873-16-5 |
| 167 | c.2 | 3693 | name_id | CI45350 | formula_like | 518-45-6 |
| 167 | c.2 | 3694 | name_id | CI45380 | formula_like | 17372-87-1 |
| 167 | c.2 | 3695 | name_id | CI45396 | formula_like | 24545-86-6 |
| 167 | c.2 | 3696 | name_id | CI45405 | formula_like | 6441-77-6 |
| 167 | c.2 | 3697 | name_id | CI45410 | formula_like |  |
| 167 | c.2 | 3698 | name_id | CI47005 | formula_like | 8004-92-0 |
| 167 | c.2 | 3699 | name_id | CI50325 | formula_like | 6837-46-3 |
| 167 | c.2 | 3700 | name_id | CI50420 | formula_like | 1064-48-8 |
| 167 | c.2 | 3701 | name_id | CI60724 | formula_like | 19286-75-0 |
| 167 | c.2 | 3702 | name_id | CI60730 | formula_like | 4430-18-6 |
| 167 | c.2 | 3703 | name_id | CI61570 | formula_like | 4403-90-1 |
| 167 | c.2 | 3704 | name_id | CI61585 | formula_like | 4474-24-2 |
| 167 | c.2 | 3705 | name_id | CI62045 | formula_like | 5617-28-7 |
| 167 | c.2 | 3706 | name_id | CI69800 | formula_like | 81-77-6 |
| 167 | c.2 | 3707 | name_id | CI69825 | formula_like | 130-20-1 |
| 168 | c.2 | 3709 | name_id | Cl73000 | formula_like | 482-89-3 |
| 168 | c.2 | 3710 | name_id | Cl73015 | formula_like | 860-22-0 |
| 168 | c.2 | 3711 | name_id | CI73360 | formula_like | 2379-74-0 |
| 168 | c.2 | 3712 | name_id | CI73385 | formula_like | 5462-29-3 |
| 168 | c.2 | 3713 | name_id | CI73900 | formula_like | 1047-16-1 |
| 168 | c.2 | 3714 | name_id | CI73915 | formula_like | 980-26-7 |
| 168 | c.2 | 3715 | name_id | CI74100 | formula_like | 574-93-6 |
| 168 | c.2 | 3716 | name_id | CI74180 | formula_like | 1330-38-7 |
| 168 | c.2 | 3717 | name_id | CI75170 | formula_like | 73-40-5 |
| 168 | c.2 | 3718 | name_id | CI75810 | formula_like | 11006-34-1 |
| 168 | c.2 | 3719 | name_id | Cl77000 | formula_like | 7429-90-5 |
| 168 | c.2 | 3720 | name_id | CI77002 | formula_like | 14762-49-3 |
| 168 | c.2 | 3721 | name_id | CI77004 | formula_like | 1327-36-2 |
| 168 | c.2 | 3722 | name_id | Cl77007 | formula_like | 12769-96-9 |
| 168 | c.2 | 3723 | name_id | CI77015 | formula_like | 1309-37-1 |
| 168 | c.2 | 3724 | name_id | CI77120 | formula_like | 7727-43-7 |
| 168 | c.2 | 3725 | name_id | CI77163 | formula_like | 7787-59-9 |
| 168 | c.2 | 3726 | name_id | CI77288 | formula_like | 1308-38-9 |
| 168 | c.2 | 3727 | name_id | CI77289 | formula_like | 12001-99-9 |
| 168 | c.2 | 3728 | name_id | Cl77346 | formula_like | 1345-16-0 |
| 168 | c.2 | 3729 | name_id | Cl77400 | formula_like | 7440-50-8 |
| 168 | c.2 | 3730 | name_id | CI77480 | formula_like | 7440-57-5 |
| 168 | c.2 | 3731 | name_id | CI77489 | formula_like |  |
| 168 | c.2 | 3732 | name_id | CI77492 | formula_like | 51274-00-1 |
| 168 | c.2 | 3733 | name_id | Cl77510 | formula_like | 14038-43-8 |
| 168 | c.2 | 3734 | name_id | CI77742 | formula_like | 2466-09-3 |
| 169 | c.2 | 3735 | name_id | CI77745 | formula_like | 10124-54-6 |
| 174 | c.2 | 3868 | name_id | Cl42090 | formula_like | 3844-45-9 |
| 174 | c.2 | 3869 | name_id | CI45380 | formula_like | 15086-94-9, 15876-39-8, 17372-87-1, 548-26-5, 94021-89-3 |
| 174 | c.2 | 3871 | name_id | CI75470 | formula_like | 1390-65-4 |
| 174 | c.2 | 3872 | name_id | CI77007 | formula_like | 12769-96-9, 1302-83-6, 57455-37-5 |
| 174 | c.2 | 3873 | name_id | CI77491 | formula_like | 1309-37-1, 1317-61-9, 1345-25-1, 1345-27-3, 52357-70-7 |
| 175 | c.2 | 3874 | name_id | CI77492 | formula_like | 1345-27-3, 20344-49-4, 52357-70-7 |
| 175 | c.2 | 3875 | name_id | CI77499 | formula_like | 12227-89-3, 1309-37-1, 1317-61-9, 1345-25-1, 1345-27-3, 52357-70-7 |
| 175 | c.2 | 3876 | name_id | Cl77510 | formula_like | 12240-15-2, 25869-00-5 |
| 175 | c.2 | 3877 | name_id | CI77742 | formula_like | 10101-66-3 |
| 175 | c.2 | 3878 | name_id | Cl77891 | formula_like | 13463-67-7 |
| 177 | c.2 | 3922 | name_id | 2-methoxy-4-(methoxymethyl)phenol | ocr_intraword_closer | 5533-03-9 |
| 178 | c.2 | 3941 | name_id | }:&gt;-Methylhydrocinnamic Aldehyde | unbalanced_brackets, ocr_noise, repeated_special_characters | 5406-12-2 |
| 178 | c.2 | 3949 | name_id | 1-Amino-2-Nitro-4-(2",3"-Dihydroxypropyl) -Amino-5-Chlorobenzene | repeated_special_characters |  |
| 178 | c.2 | 3955 | name_id | Nn"N"-Tris(Polyoxyethylene)- N-Hexadecylpropylene Diamine Dihydrofluoride | repeated_special_characters |  |
| 179 | c.2 | 3961 | name_id | El 71 Titanium Dioxide (C.I. 77891) | ocr_intraword_dot |  |
| 179 | c.2 | 3962 | name_id | E172 Iron Oxides, Iron Hydroxides Yellow /Brown-C.I. 77492; Red; 7491; Brown; 77 499) | unbalanced_brackets, ocr_intraword_dot |  |
| 180 | c.2 | 3991 | name_id | E339a Sodium Dihydrogen Orthophosphate | ocr_intraword_digit |  |
| 180 | c.2 | 3992 | name_id | E339b Disodium Hydrogen Orthophosphate | ocr_intraword_digit |  |
| 180 | c.2 | 3993 | name_id | E339c Trisodium Hydrogen Orthophosphate | ocr_intraword_digit |  |
| 180 | c.2 | 3994 | name_id | E340a Potassium Dihydrogen Orthophosphate | ocr_intraword_digit |  |
| 180 | c.2 | 3995 | name_id | E340b diPotassium Hydrogen Orthophosphate | ocr_intraword_digit |  |
| 180 | c.2 | 3996 | name_id | E340c triPotassium Orthophosphate | ocr_intraword_digit |  |
| 181 | c.2 | 4009 | name_id | E440b Amidated Pectin | ocr_intraword_digit |  |
| 181 | c.2 | 4011 | name_id | E450a,b,c Sodium and Potassium Phosphates and Polyphosphates | ocr_intraword_digit |  |

## OCR 재판독 보정

2026-11-15, `/Users/dany/glhac-wt-residual`의 `fix/polish-notify-parse`에서 수행했다.
앞 절의 수치·제외 표는 당시 기록으로 유지하며, 이번 보정 이후 집계는 이 절을 따른다.

입력은 다음 디렉터리의 `candidates.tsv`, `crops/`, `audit/manifest.json`, `audit/ocr/`다.

`/private/tmp/claude-501/-Users-dany-IdeaProjects-llmdev/c9e702da-a2c7-4fd5-bf64-d8685f495f89/scratchpad/kma_ocr/`

오케스트레이터가 likely_fix 표본 48건을 원본 이미지와 대조해 정확함을 확인했다는
검토 결과를 사용했다. 283건 전체를 사람이 시각 검수했다는 의미는 아니다.
이번에는 전체 후보의 원래 필드·PDF 텍스트 레이어·크롭 높이·OCR 줄수 및 경계 여백을
비교하고, 이름 조각·길이 단축·괄호와 정규화 충돌을 추가 점검했다.
경계 점검 대상인 No.298, 2210과 이름 조각 No.1866 등의 크롭을 직접 확인했다.
No.2210의 두 번째 텍스트 레이어 줄은 잡음이며, 크롭에는 완전한 한 줄 이름이 보인다.

고정 보정표는 [`app/data/kma1360_ocr_fixes.tsv`](../app/data/kma1360_ocr_fixes.tsv)다.
likely_fix **283건 전부**에 section/no/page/field, 원래값(`original`), 보정 후보(`corrected`),
CAS, 반영 여부, 제외 사유, `OCR 재판독` 근거, 크롭 절대경로·SHA256·크기,
원시 OCR JSON 경로 및 검토 메모를 남겼다. 임시 입력 이미지가 없어져도 보정표만으로
JSON 재생성이 가능하다. 원본 이미지 재검토에는 위 입력 디렉터리가 필요하다.

| 처리 | 이름 필드 수 |
| --- | ---: |
| likely_fix 반영 | 269 |
| likely_fix 제외 | 14 |
| unclear, 미반영 | 88 |
| same, 미반영 | 66 |

제외 사유는 다음과 같으며 이번 집계에서 중복은 없다.

| 사유 | 수 | 대상 및 판단 |
| --- | ---: | --- |
| `multiline_fragment` | 1 | c.2 No.1866: `-Dimethyl-…`로 시작하는 이름 조각. 완전한 이름 확인 전 미반영 |
| `different_cas_collision` | 12 | CI 42090, 45380, 77007, 77492, 77510, 77742의 각 두 행. 정규화 이름은 같으나 CAS 집합이 다름 |
| `unknown_cas_collision` | 1 | c.2 No.2986의 후보가 No.3776과 정규화 충돌. 전자는 CAS 15716-30-0, 후자는 CAS 없음이므로 동일성 확인 불가 |

CAS 충돌 검사는 기존 두 이름 필드와 모든 likely_fix 보정 후보를 함께 비교했다.
따라서 두 후보끼리 충돌하는 경우도 양쪽을 제외한다. CAS 집합이 일부 겹치더라도
서로 같지 않으면 보수적으로 제외했다. 충돌 쌍은 No.3684/3868, 3694/3869,
3722/3872, 3732/3874, 3733/3876, 3734/3877이다.
나머지 후보에서 닫는 괄호·소문자로 시작하는 조각, 괄호 불균형,
공백 제거 후 원래 필드 대비 90% 미만으로 짧아진 이름은 없었다.
기존 name_en을 포함해 크롭한 경우에는 그 별칭을 제외한 동일 필드끼리 길이를 비교했다.
추가로 윗줄이 잘린 것으로 판정한 후보는 없었다.

반영한 필드는 후보 문자열 그대로 저장하고, 이전 값을 `name_id_ocr_raw` 또는
`name_en_ocr_raw`로 보존한다. 보정표에 없는 필드와 제외한 14건은 그대로 유지했다.
모든 `name_index`는 기존 `excluded_name_metadata()`로 다시 계산하며, 보정 후에도
기존 제외 규칙에 걸리는 이름을 허용하지 않는다. a/b 범주 설명 5건도 보정했지만
화학물질 이름 색인에는 포함하지 않는다.

| 집계 | 보정 전 | 보정 후 | 증감 |
| --- | ---: | ---: | ---: |
| 전체 이름 필드 | 4,619 | 4,619 | 0 |
| 기존 이름 규칙을 통과한 필드(a/b 포함) | 4,182 | 4,451 | +269 |
| `name_index` 제외 이름 | 437 | 168 | -269 |
| 실제 화학물질 색인 이름(c.1/c.2, 별칭 포함) | 4,104 | 4,368 | +264 |
| 실제 화학물질 색인 고유 정규화 키 | 3,990 | 4,252 | +262 |
| 유효 CAS 연결 | 3,905 | 3,905 | 0 |
| 고유 CAS | 3,591 | 3,591 | 0 |

[`scripts/build_kma1360.py`](../scripts/build_kma1360.py)는 CLI 재생성 시 고정 보정표를
기본 적용한다. 모든 대상의 위치·CAS·원래값·중복 여부를 먼저 검증하고 한 건이라도
맞지 않으면 변경 전에 실패한다. 이미 적용된 행은 보정값과 원문 보존값이 모두 같아야
재적용을 허용한다. `extract_document()` 자체는 원문 추출과 기존 별칭 분리 규칙을
유지한다. 현재 분리 규칙이 `.Pure …`를 원문 이름에 합쳐 두는 No.1184/1185는
보정표의 기존 다른 이름(`other_name`)과 원래 별칭이 정확히 일치하고, 보정된 영문명이
기존 영문 별칭 검사를 통과할 때만 이전 필드 경계를 복원한다. 다른 이름은 재분리하지 않는다.

원본 PDF 182쪽에서 CLI로 재생성한 결과와 저장 JSON이 바이트 단위로 일치한다.
4,112행의 no/section/page/CAS는 모두 유지했다. 원문 보존 필드로 복원한 보정 전
JSON도 아래 기존 SHA256과 일치하며, unclear 88건·same 66건을 포함한 모든 원문을 검증한다.

- 보정 전 JSON SHA256: `047d7ef613eb64b54a5c723ae88173c4783624790eee84c27967d0e5ba4c878a`
- 보정 후 JSON SHA256: `97157fbbc95b22bf510626950d554af6d61b30592b02636fa9603e282ec43261`
- 보정표 SHA256: `316722ccabb82d4095811622b688a907e1d1ee0d91487bc20bf8695268cb6af3`
- 원문 PDF SHA256: `fa3562bbd86256a09f91ad18014421fe62914a20cff5055c2b6a714d790f531b`

런타임 `app/screening.py`, `app/kma1360.py`와 기존 알림·파싱 미커밋 변경은 수정하지 않았다.
신규 [`tests/test_kma1360_ocr_fixes.py`](../tests/test_kma1360_ocr_fixes.py)는 원문 추적·재생성,
전수 색인 일관성·CAS 보존, 미반영 충돌, 옛 오독 문자열 차단을 검증한다.
`2-Hepten-4-One`, `Klobetasol Propionat (Clobetasol Propionate)`, `CI 77002`,
`Pure Myrrh Gum`의 매칭과 CAS 일치, `unmatched_default`만 승격, 사전 우선,
주석·부가 문맥 차단도 검증한다. `NaaP04`, `2-Ethylbutyra).dehyde`,
`Hydroxyphenyllf'ropionate`와 보정 전 269개 문자열은 런타임 별칭으로 등록되지 않는다.

검증 명령과 결과:

```sh
GLHAC_DEV=1 GLHAC_NOTIFY_WORKER=0 GLHAC_RATE_LIMIT_PER_MIN=0 .venv/bin/python -m pytest -q -p no:cacheprovider tests/
for f in tests/*.test.cjs; do node "$f" >/dev/null || echo FAIL $f; done
```

- Python: **10,699 passed, 2 failed**, 22 warnings, 767.84초. 신규 OCR 테스트 313개는 모두 통과했다.
- Node: **4개 파일 통과, 실패 0**.
- PDF 재생성 바이트 일치, 보정표 283건의 크롭 해시·OCR 근거 경로, 원문 PDF 해시 검증 통과.
- 기존 알림·파싱 변경과 런타임 안전 규칙 파일의 작업 전후 해시가 동일하다.
- 기존 KMA 테스트 4개 파일을 변경하지 않았다. 운영 접속·배포·커밋·브랜치 변경은 하지 않았다.

실패 2건은 기존 데이터 계약과 이번 데이터 보정의 충돌이다.

| 기존 테스트 | 실패 지점 | 충돌 |
| --- | --- | --- |
| `test_kma1360.py::test_catalogue_completeness_provenance_and_all_cas_digits` | 176행 | 정확히 6필드만 허용하는 검증이 요청된 `*_ocr_raw` 필드 추가를 거절 |
| `test_kma1360_p1_round3.py::test_current_catalogue_all_exclusions_aliases_and_cas_are_consistent` | 179행 | 보정 전 제외 437개를 고정 검증하나 보정 후에는 168개 |

기존 안전 반례의 추가 실패는 없다. 기존 테스트 기대값 및 수정 범위 제한을 유지했으므로
**전체 실패 0 조건은 아직 충족하지 못했다**. 두 데이터 검증의 기대값을 유지하면서
보정 전 원문 스냅샷을 대상으로 실행하도록 바꾸려면 기존 테스트 수정 범위의 승인이 필요하다.

보정표 파일은 생성되어 있으나 `.gitignore:11`의 `/app/data/*` 규칙으로 Git 변경 목록에는
나타나지 않는다. 지정 범위 밖의 `.gitignore` 및 Git 인덱스는 변경하지 않았다.
보정표를 버전 관리에 포함하려면 `!/app/data/kma1360_ocr_fixes.tsv` 예외 또는 별도 강제 추가가 필요하다.


## 검수 1차 반려 후 혼동문자 재감사 (2026-11-15)

이 절은 위 OCR 재판독 보정 절의 최초 집계·검증 결과를 갱신한다. 최초 기록은 이력으로
남긴다. cli-v2는 Port 1에서 API 호스트 이름 조회 실패로 REJECT하여 지침의 fallback을
사용했다. 작업 전 미커밋 내용은 `/private/tmp/polish-fix1-baseline/`에 보존했다.

### 지적된 재오독 5건의 처리

5건 모두 같은 원본 크롭과 `audit/ocr/` 원시 OCR을 대조했다. 원시 OCR의 대문자 I/숫자 1과
달리 인쇄된 글자는 소문자 l이다. 따라서 보정 제외 없이 이름을 바로잡았다. 아래 원시 OCR은
수정하지 않았으며, `*_ocr_raw`는 PDF 텍스트 레이어의 원래값을 그대로 보존한다.

| No. / 쪽 | 검수 전 보정·원시 OCR의 오류 | 크롭 판독 및 최종 보정값 | 처리 |
| --- | --- | --- | --- |
| 1238 / 65 | `-YI` | `1-(3,3-Dimethylbicyclo[2.2.1]Hept-2-Yl) Ethane-1-One` | 소문자 l로 수정·반영 유지 |
| 1578 / 80 | `-O1` | `(2E,4E)-Octa-2,4-Dien-1-Ol` | 소문자 l로 수정·반영 유지 |
| 1579 / 80 | `-O1` | `2,4-Nonadien-1-Ol` | 소문자 l로 수정·반영 유지 |
| 1580 / 80 | `-O1` | `(2E,4E)-Deca-2,4-Dien-1-Ol` | 소문자 l로 수정·반영 유지 |
| 3463 / 156 | `-O1` | `4-Methyl-8-Methylene Tricyclo(3.3.1.1 3.7)Decan-2-Ol` | 소문자 l로 수정·반영 유지 |

### 269건 전수 선별 및 크롭 판정

적용 중이던 269건 모두에 대해 원래값과 검수 전 보정값에서 공백만 제거하고,
`{l,1,I,|}`와 `{O,o,0}`를 각각 같은 비교 문자로 바꾼 후 `SequenceMatcher(autojunk=False)`로
정렬했다. 같은 비교 구간 내 실제 문자가 달라진 항목을 전부 후보로 삼았다. 이 방법은
괄호·잡음 제거와 동시에 일어난 혼동도 포착한다(예: No.493의 `:0ne→One`).
보정값의 `O1/OI/Y1/YI` 토큰 말미·소문자 사이 대문자 I도 별도로 전수 검색했다.
화학 접미·단어 내부에 한정하지 않고 위치 번호·CI 코드까지 후보에 포함해 대조했다.
공백만 달라진 행은 혼동문자 교체 건수에서 제외했다.

| 구분 | 수 | 판정 |
| --- | ---: | --- |
| 전수 비교한 적용 보정 | 269 | 원래값·보정값·원시 OCR 확인, 크롭 해시 모두 일치 |
| 공백 제외 시 혼동문자만 다른 후보 | 111 | 아래 표에서 대조 |
| 다른 수정과 혼동문자 교체가 섞인 후보 | 25 | 아래 표에서 대조 |
| 재대조 후보 합계 | 136 | 136개 크롭과 원시 OCR 직접 대조 |
| 후보 중 기존 보정 유지 | 131 | 혼동문자 위치의 인쇄 글자와 보정값 일치 |
| 후보 중 재보정 | 5 | 위 1238·1578·1579·1580·3463 |
| 후보 중 추가 보정 제외 | 0 | 해당 없음 |
| 혼동문자 교체 후보가 아닌 적용 보정 | 133 | 이 재감사의 크롭 직접 대조 대상은 아님 |

이 감사는 지정 혼동문자의 위치와 판독에 대한 검증이며 모든 이름의 화학적 적합성을 새로
인증하는 것은 아니다. 전체 283건의 보정 상태는 **반영 269 / 제외 14**로 유지된다.
아래 항목은 모두 c.2, name_id이며, 근거 경로는 앞 절의 `kma_ocr/` 아래
`crops/{No.}_name_id.png`와 `audit/ocr/{No.}_name_id.json`이다.
원래값은 PDF 텍스트 레이어, 화살표 오른쪽은 이번 재검수 **직전** 보정값이다.
'유지'는 크롭·원시 OCR의 해당 혼동문자와 일치한다는 판정이다.

| No. / 쪽 | 원래값 → 검수 전 보정값 | 크롭 대조 판정 |
| --- | --- | --- |
| 296 / 24 | `Salbutamol Su1fat` → `Salbutamol Sulfat` | 유지 |
| 361 / 26 | `((3a,4,5,6,7,7a.:.Hexahydro-4,7- Methano -lh-Inden-5(0r 6)- Yl)Oxy)` → `((3a,4,5,6,7,7a-Hexahydro-4,7- Methano -1h-Inden-5(Or 6)- Yl)Oxy)` | 유지 |
| 366 / 27 | `1-(1,2,3,5,6, 7,8,8a-Octahydro- 2,3,8,8-Tetramethyl-2-Naphthyl)Ethan-1-0ne` → `1-(1,2,3,5,6,7,8,8a-Octahydro-2,3,8,8-Tetramethyl-2-Naphthyl)Ethan-1-One` | 유지 |
| 367 / 27 | `1-(2,4,4-Trimethyl-1-Cyclohex- 2-Enyl)But-2-En-1-0ne` → `1-(2,4,4-Trimethyl-1-Cyclohex- 2-Enyl)But-2-En-1-One` | 유지 |
| 368 / 27 | `1-(2,4-Dimethylcyclohex-3-En- 1-Yl)- 2,2-Dimethylpropan-1-0ne` → `1-(2,4-Dimethylcyclohex-3-En- 1-Yl)- 2,2-Dimethylpropan-1-One` | 유지 |
| 372 / 27 | `2-Cyclohexylhepta-1,6-Dien-3-0ne` → `2-Cyclohexylhepta-1,6-Dien-3-One` | 유지 |
| 438 / 29 | `3-Methyl-4-Phenyl-3-Buten-2-0ne` → `3-Methyl-4-Phenyl-3-Buten-2-One` | 유지 |
| 449 / 30 | `(E)-1-(2,6,6-Trimethylcyclohex- 2-En- 1-Yl)Pent-1-En-3-0ne` → `(E)-1-(2,6,6-Trimethylcyclohex- 2-En- 1-Yl)Pent-1-En-3-One` | 유지 |
| 460 / 30 | `3-Ethyl-2-Hydroxycyclopent-2- En-1-0ne` → `3-Ethyl-2-Hydroxycyclopent-2- En-1-One` | 유지 |
| 475 / 31 | `2,6-0ctadienoic Acid, 3,7- Dimethyl, Ethyl Ester 2-Hexylcyclopent-2-En-1-0ne` → `2,6-Octadienoic Acid, 3,7- Dimethyl, Ethyl Ester 2-Hexylcyclopent-2-En-1-One` | 유지 |
| 479 / 31 | `(E)-1-(2,6,6-Trimethyl-1- Cyclohexenyl) But-2-En-1-0ne` → `(E)-1-(2,6,6-Trimethyl-1- Cyclohexenyl) But-2-En-1-One` | 유지 |
| 493 / 32 | `(E)-1-(2,6,6-Trimethyl-1- Cyclohexa- 1,3-Dienyl)But-2- En-1-:0ne` → `(E)-1-(2,6,6-Trimethyl-1- Cyclohexa- 1,3-Dienyl)But-2- En-1-One` | 유지 |
| 509 / 32 | `(2e)-2-Heptylidenecyclopentan-1-0ne` → `(2e)-2-Heptylidenecyclopentan-1-One` | 유지 |
| 564 / 35 | `1-( 1,2,3,4,6, 7 ,8,8a-Octahydro-2,3,8,8 -Tetramethyl-2- Naphthyl] Ethan-1-0ne` → `1-(1,2,3,4,6,7,8,8a-Octahydro-2,3,8,8 -Tetramethyl-2- Naphthyl) Ethan-1-One` | 유지 |
| 566 / 35 | `8,8-Dimethyl-2,3.,4,5,6, 7- Hexahydro -lH-Naphthalene-2- Carbaldehyde` → `8,8-Dimethyl-2,3,4,5,6,7- Hexahydro -1H-Naphthalene-2- Carbaldehyde` | 유지 |
| 661 / 39 | `2 ,4,4, 7 -Tetramethyloct-6-En-3-0ne` → `2,4,4,7-Tetramethyloct-6-En-3-One` | 유지 |
| 665 / 39 | `4-Phenylbutan-2-0ne` → `4-Phenylbutan-2-One` | 유지 |
| 680 / 40 | `3-Methyl-2-Pentylcyclopentan-1-0ne` → `3-Methyl-2-Pentylcyclopentan-1-One` | 유지 |
| 688 / 40 | `(E)-3-Methyl-4-(2 ,4 ,6-Trimethylcyclohex -3-En-1- Yl)But-3- En-2-0ne` → `(E)-3-Methyl-4-(2,4,6-Trimethylcyclohex -3-En-1- Yl)But-3- En-2-One` | 유지 |
| 691 / 40 | `2-Hexylidenecyclohexan-1-0ne` → `2-Hexylidenecyclohexan-1-One` | 유지 |
| 697 / 40 | `1-Hydroxydecan-3-0ne` → `1-Hydroxydecan-3-One` | 유지 |
| 711 / 41 | `8-Cyclohexadecen-1-0ne` → `8-Cyclohexadecen-1-One` | 유지 |
| 717 / 41 | `Cedr-8-En-10-0ne` → `Cedr-8-En-10-One` | 유지 |
| 730 / 42 | `4,6,6-Trimethylbicyclo[3.1. l]Hept-3- En-2-0ne` → `4,6,6-Trimethylbicyclo[3.1.1]Hept-3-En-2-One` | 유지 |
| 733 / 42 | `(E)-1-(2,2-Dimethyl-6 Methylidenecyclohexyl) But-2- En-1-0ne` → `(E)-1-(2,2-Dimethyl-6 Methylidenecyclohexyl) But-2- En-1-One` | 유지 |
| 770 / 43 | `6-Undecyloxan-2-0ne` → `6-Undecyloxan-2-One` | 유지 |
| 804 / 45 | `2-[[(ls,4s,6s)-1,7,7-Trimethyl-6- Bicyclo [2.2.1 ]Heptanyl)Oxy)Eth anol` → `2-[[(1s,4s,6s)-1,7,7-Trimethyl-6- Bicyclo [2.2.1]Heptanyl]Oxy]Eth anol` | 유지 |
| 868 / 48 | `2-(2-(4-Methylcyclohex-3-En-1-Yl)Propyl]Cyclopentan-1-0ne` → `2-[2-(4-Methylcyclohex-3-En-1-Yl)Propyl]Cyclopentan-1-One` | 유지 |
| 880 / 48 | `2 ,6-Dimethyloct- 7 -En-4-0ne` → `2,6-Dimethyloct-7-En-4-One` | 유지 |
| 894 / 49 | `1, 7 -Dioxacycloheptadecan-8-0ne` → `1,7-Dioxacycloheptadecan-8-One` | 유지 |
| 896 / 49 | `1,8-Dioxacycloheptadecan-9-0ne` → `1,8-Dioxacycloheptadecan-9-One` | 유지 |
| 906 / 49 | `(E)-4-(2,5,6,6- Tetramethylcyclohexen-1-Yl)But-3- En-2-0ne` → `(E)-4-(2,5,6,6- Tetramethylcyclohexen-1-Yl)But-3- En-2-One` | 유지 |
| 915 / 50 | `1, 1,2,3,3-Pentamethyl-2,5,6, 7-Tetrahydroinden-4-0ne` → `1,1,2,3,3-Pentamethyl-2,5,6,7-Tetrahydroinden-4-One` | 유지 |
| 992 / 53 | `4, 7 -Dimethyloct-6-En-3-0ne` → `4,7-Dimethyloct-6-En-3-One` | 유지 |
| 1033 / 55 | `14h-Benz[4,5]Isoquino[2, 1- A]Perimidin-14-0ne` → `14h-Benz[4,5]Isoquino[2,1- A]Perimidin-14-One` | 유지 |
| 1058 / 56 | `N-(l,5-Dimethyl-8- Bicyclo(3.2.1] Octanylidene)Hydroxyl Amine` → `N-(1,5-Dimethyl-8- Bicyclo[3.2.1] Octanylidene)Hydroxyl Amine` | 유지 |
| 1071 / 57 | `4-( 1-Ethoxyethenyl)-3,3,5,5-Tetramethylcyclohexan-1-0ne` → `4-(1-Ethoxyethenyl)-3,3,5,5-Tetramethylcyclohexan-1-One` | 유지 |
| 1072 / 57 | `(3e)-Oxacyclohexadec-3-En-2-0ne` → `(3e)-Oxacyclohexadec-3-En-2-One` | 유지 |
| 1082 / 57 | `(Z)-1-(2 ,6,6-Trimethylcycl~hexen-1-Yl)But- 2-En-1-0ne` → `(Z)-1-(2,6,6-Trimethylcyclohexen-1-Yl)But-2-En-1-One` | 유지 |
| 1096 / 58 | `1-(2-Methyl-5-Propan-2- Ylcyclohex- 2-En-1-Yl)Propan-1-0ne` → `1-(2-Methyl-5-Propan-2- Ylcyclohex- 2-En-1-Yl)Propan-1-One` | 유지 |
| 1120 / 59 | `3-Methyl-3-Penten-2-0ne` → `3-Methyl-3-Penten-2-One` | 유지 |
| 1131 / 60 | `5-Cyclohexadecen-1-0ne` → `5-Cyclohexadecen-1-One` | 유지 |
| 1134 / 60 | `2 ,6,8-Trimethylnonan-4-0ne` → `2,6,8-Trimethylnonan-4-One` | 유지 |
| 1138 / 60 | `(Z)-1-(2,6,6-Trimethylcyclohex- 2-En- 1-Yl)But-2-En-1-0ne` → `(Z)-1-(2,6,6-Trimethylcyclohex- 2-En- 1-Yl)But-2-En-1-One` | 유지 |
| 1200 / 63 | `Trans-P-Menthan-3-0ne` → `Trans-P-Menthan-3-One` | 유지 |
| 1232 / 64 | `4,8-Dimethyl-2-Propan-2- Ylidene-3,3a,4,5,6,8a-Hexahydro-lh-Azulen-6-0l` → `4,8-Dimethyl-2-Propan-2- Ylidene-3,3a,4,5,6,8a-Hexahydro-1h-Azulen-6-Ol` | 유지 |
| 1238 / 65 | `1-{3,3-Dimethylbicyclo[2.2.1 ]Hept-2-Yl) Ethane-1-0ne` → `1-(3,3-Dimethylbicyclo[2.2.1]Hept-2-YI) Ethane-1-One` | `-Yl`로 재보정 |
| 1246 / 65 | `1-( 1,2 ,3, 4-Tetrahydro-4, 4- Dimethyl- 1-Naphthyl)Propan-1-0ne` → `1-(1,2,3,4-Tetrahydro-4,4- Dimethyl- 1-Naphthyl)Propan-1-One` | 유지 |
| 1326 / 68 | `10-Dodecen-3-0ne, 5-Hydroxy- 7,11-Dimethyl-` → `10-Dodecen-3-One, 5-Hydroxy- 7,11-Dimethyl-` | 유지 |
| 1329 / 69 | `lh-Inden-1-0ne, 3A,4,5,6, 7, 7 A-Hexahydro-3,3A, 7, 7-Tetramethyl-` → `1h-Inden-1-One, 3A,4,5,6,7,7A-Hexahydro-3,3A,7,7-Tetramethyl-` | 유지 |
| 1342 / 69 | `2-(3-Heptyl}- l ,3-Dioxolane` → `2-(3-Heptyl)-1,3-Dioxolane` | 유지 |
| 1351 / 70 | `2, 7 -Dimethyloct-5-En-4-0ne` → `2,7-Dimethyloct-5-En-4-One` | 유지 |
| 1352 / 70 | `2-[(3,3,5-Trimethylcyclohexyl)Acetyl]Cyc lopen Tan-1-0ne` → `2-[(3,3,5-Trimethylcyclohexyl)Acetyl]Cyc lopen Tan-1-One` | 유지 |
| 1356 / 70 | `1-Phenyl-4-Penten-1-0ne` → `1-Phenyl-4-Penten-1-One` | 유지 |
| 1374 / 71 | `(E)-Oct-5-En-2-0ne` → `(E)-Oct-5-En-2-One` | 유지 |
| 1387 / 71 | `5-Phenylhex-3-En-2-0ne` → `5-Phenylhex-3-En-2-One` | 유지 |
| 1389 / 71 | `7 -Cyclohexadecen-1-0ne` → `7-Cyclohexadecen-1-One` | 유지 |
| 1399 / 72 | `4-Methyl-1-0~aspiro[5.5]Undecene` → `4-Methyl-1-Oxaspiro[5.5]Undecene` | 유지 |
| 1409 / 72 | `4,9, 12, 12-Tetramethyl-5-0xatricyclo (8.2.0.04,6] Dodecane` → `4,9,12,12-Tetramethyl-5-Oxatricyclo [8.2.0.04,6] Dodecane` | 유지 |
| 1423 / 73 | `2-Cyclopenten-1-0ne, 2- Hydroxy-3,4-Dimethyl-` → `2-Cyclopenten-1-One, 2- Hydroxy-3,4-Dimethyl-` | 유지 |
| 1433 / 74 | `2-Ethyl-4,4- Dimethylcyclohexan-1-0ne` → `2-Ethyl-4,4- Dimethylcyclohexan-1-One` | 유지 |
| 1441 / 74 | `6,6,6a-Trimethyl-3,3a,4,5-Tetrahydrocyclopenta[B]Furan- 2-0ne` → `6,6,6a-Trimethyl-3,3a,4,5-Tetrahydrocyclopenta[B]Furan- 2-One` | 유지 |
| 1475 / 76 | `Indan-2-0ne` → `Indan-2-One` | 유지 |
| 1481 / 76 | `(4z)-Cyclopentadec-4-En-1-0ne` → `(4z)-Cyclopentadec-4-En-1-One` | 유지 |
| 1485 / 76 | `l-(l,2,8,8-Te'f:!a.II1ethyl-l,3,4,5,6,7-Hexahydronaphthalen-2- Yl)Ethanone` → `1-(1,2,8,8-Tetramethyl-1,3,4,5,6,7-Hexahydronaphthalen-2- Yl)Ethanone` | 유지 |
| 1498 / 77 | `5,6, 7 -Trimethylocta-2 ,5-Dien-4-0ne` → `5,6,7-Trimethylocta-2,5-Dien-4-One` | 유지 |
| 1573 / 80 | `P-Mentha-1,4(8)-Dien-3-0ne, (2,4- Dinitrophenyl)Hydrazone (En)` → `P-Mentha-1,4(8)-Dien-3-One, (2,4- Dinitrophenyl)Hydrazone (En)` | 유지 |
| 1578 / 80 | `(2E,4E)-Octa-2,4-Dien-1-0l` → `(2E,4E)-Octa-2,4-Dien-1-O1` | `-Ol`로 재보정 |
| 1579 / 80 | `2,4~Nonadien-1-0l` → `2,4-Nonadien-1-O1` | `-Ol`로 재보정 |
| 1580 / 80 | `(2E,4E)-Deca-2,4-Dien-1-0l` → `(2E,4E)-Deca-2,4-Dien-1-O1` | `-Ol`로 재보정 |
| 1792 / 88 | `2-Hydroxy-2-Cyclohexen-1-0ne` → `2-Hydroxy-2-Cyclohexen-1-One` | 유지 |
| 1793 / 88 | `2-Hydroxy-3,5,5-Trimethyl-2-Cyclohexen -1-0ne` → `2-Hydroxy-3,5,5-Trimethyl-2-Cyclohexen -1-One` | 유지 |
| 1817 / 89 | `1-(4-Methoxyphenyl)-4-Methyl- 1-Penten-3-0ne` → `1-(4-Methoxyphenyl)-4-Methyl- 1-Penten-3-One` | 유지 |
| 1821 / 89 | `3-Methylcyclopent-2-En-1-0ne` → `3-Methylcyclopent-2-En-1-One` | 유지 |
| 1823 / 89 | `3-Methyl-2-Cyclohexen-1-0ne` → `3-Methyl-2-Cyclohexen-1-One` | 유지 |
| 1825 / 89 | `5-Methyl-5-Hexen-2-0ne` → `5-Methyl-5-Hexen-2-One` | 유지 |
| 1826 / 89 | `6, 1 O-Dimethyl-5,9-Undecadien-2-0ne` → `6,10-Dimethyl-5,9-Undecadien-2-One` | 유지 |
| 1828 / 90 | `3-Penten-2-0ne` → `3-Penten-2-One` | 유지 |
| 1829 / 90 | `2-Hepten-4-0ne` → `2-Hepten-4-One` | 유지 |
| 1830 / 90 | `3-Hepten-2-0ne` → `3-Hepten-2-One` | 유지 |
| 1831 / 90 | `2-0cten-4-0ne` → `2-Octen-4-One` | 유지 |
| 1832 / 90 | `3-Decen-2-0ne` → `3-Decen-2-One` | 유지 |
| 1833 / 90 | `4-Methyl-3-Penten-2-0ne` → `4-Methyl-3-Penten-2-One` | 유지 |
| 1834 / 90 | `5-Methyl-3-Hexen-2-0ne` → `5-Methyl-3-Hexen-2-One` | 유지 |
| 1835 / 90 | `6-Methyl-3,5-Heptadien-2-0ne` → `6-Methyl-3,5-Heptadien-2-One` | 유지 |
| 1836 / 90 | `(E)-7-Methyl-3-0cten-2-0ne` → `(E)-7-Methyl-3-Octen-2-One` | 유지 |
| 1837 / 90 | `3-Nonen-2-0ne` → `3-Nonen-2-One` | 유지 |
| 1839 / 90 | `(E)-6-Methyl-_3-Hepten-2-0ne` → `(E)-6-Methyl-3-Hepten-2-One` | 유지 |
| 1840 / 90 | `(E,E)-3,5-0ctadien-2-0ne` → `(E,E)-3,5-Octadien-2-One` | 유지 |
| 1842 / 90 | `1-0cten-3-0ne` → `1-Octen-3-One` | 유지 |
| 1843 / 90 | `2-Pentyl-1-Buten-3-0ne` → `2-Pentyl-1-Buten-3-One` | 유지 |
| 1845 / 90 | `2-Methylhepta.n-3-0ne` → `2-Methylheptan-3-One` | 유지 |
| 1850 / 90 | `2-Pentyl-3-Methyl-2- Cyclopenten-1-0ne` → `2-Pentyl-3-Methyl-2- Cyclopenten-1-One` | 유지 |
| 1855 / 91 | `4-(2-Furyl)-3-Buten-2-0ne` → `4-(2-Furyl)-3-Buten-2-One` | 유지 |
| 1860 / 91 | `4-0cten-3-0ne` → `4-Octen-3-One` | 유지 |
| 1861 / 91 | `(E)-2-Nonen-4-0ne` → `(E)-2-Nonen-4-One` | 유지 |
| 1862 / 91 | `(E)-5-Nonen-2-0ne` → `(E)-5-Nonen-2-One` | 유지 |
| 1863 / 91 | `(E)-1,5-0ctadien-3-0ne` → `(E)-1,5-Octadien-3-One` | 유지 |
| 1864 / 91 | `10-Undecen-2-0ne` → `10-Undecen-2-One` | 유지 |
| 1865 / 91 | `8-Nonen-2-0ne` → `8-Nonen-2-One` | 유지 |
| 1867 / 91 | `3-Methyl-2-[(E)-Pent-2- Enyl]Cyclopent-2-En-1-0ne` → `3-Methyl-2-[(E)-Pent-2- Enyl]Cyclopent-2-En-1-One` | 유지 |
| 1870 / 91 | `5-Pentyl-3H-Furan-2-0ne` → `5-Pentyl-3H-Furan-2-One` | 유지 |
| 1873 / 91 | `1-(4-Hydroxy-3- Methoxyphenyl)-Decan -3-0ne` → `1-(4-Hydroxy-3- Methoxyphenyl)-Decan -3-One` | 유지 |
| 1932 / 94 | `Sodium 4-Methyl-2·-0xovalerate` → `Sodium 4-Methyl-2-Oxovalerate` | 유지 |
| 1955 / 95 | `lsoamyl Isobutyrate {lsopentyl Isobutyrate)` → `Isoamyl Isobutyrate (Isopentyl Isobutyrate)` | 유지 |
| 1967 / 95 | `3,7-Dimethyl-2,6-0ctadien-1-Yl 2-Ethylbutanoa~e(Geranyl 2-Ethylbutyrate)` → `3,7-Dimethyl-2,6-Octadien-1-Yl 2-Ethylbutanoate(Geranyl 2-Ethylbutyrate)` | 유지 |
| 2210 / 104 | `(E)-2-0cten-1 YI Butyrate "'I` → `(E)-2-Octen-1-Yl Butyrate` | 유지 |
| 2321 / 108 | `5,5-Dibutyloxolan-2-0ne` → `5,5-Dibutyloxolan-2-One` | 유지 |
| 2323 / 108 | `6-Pentylpyran-2-0ne` → `6-Pentylpyran-2-One` | 유지 |
| 2368 / 110 | `4-(3-0xobutylsulfanyl)Butan-2-0ne` → `4-(3-Oxobutylsulfanyl)Butan-2-One` | 유지 |
| 2375 / 110 | `Ethyl2-Hydroxyethy1Sulfide` → `Ethyl 2-Hydroxyethyl Sulfide` | 유지 |
| 2715 / 124 | `4-Hexene-3-0ne` → `4-Hexene-3-One` | 유지 |
| 2870 / 130 | `6-Methyl.:.5-Hepten-2-0ne` → `6-Methyl-5-Hepten-2-One` | 유지 |
| 2875 / 130 | `Oxacycloheptadec-1 O-En-2-0ne` → `Oxacycloheptadec-10-En-2-One` | 유지 |
| 2913 / 131 | `2-Methyl-1-(4-Methylthiopenyl)- 2-Morpholinopropan-1-0ne` → `2-Methyl-1-(4-Methylthiopenyl)-2-Morpholinopropan-1-One` | 유지 |
| 3006 / 136 | `{l,3,5)-Triazine-2,4-Bis((4-(2-Ethyl- Hexyloxy)-2-Hydroxy)- Phenyl) -6-(4- Methoxyphenyl) (Bis-Ethylhexyloxyphenol/Methoxyp henyl Triazine)` → `(1,3,5)-Triazine-2,4-Bis((4-(2-Ethyl- Hexyloxy)-2-Hydroxy)- Phenyl) -6-(4- Methoxyphenyl) (Bis-Ethylhexyloxyphenol/Methoxyp henyl Triazine)` | 유지 |
| 3140 / 141 | `1-(4-Chlorophenoxy)-1- (Imidazol-1-Yl)3,3- Dimethylbutan-2-0ne (+)` → `1-(4-Chlorophenoxy)-1- (Imidazol-1-Yl)3,3- Dimethylbutan-2-One (+)` | 유지 |
| 3463 / 156 | `4-Methyl-8-M.ethylene Tricyclo(3.3.1.1 3. 7)Decan-2-0l` → `4-Methyl-8-Methylene Tricyclo(3.3.1.1 3.7)Decan-2-O1` | `-Ol`로 재보정 |
| 3465 / 156 | `13-0xa Bicyclo{l0.1.0)Trideca- 4,8-Diene, Trimethyl-` → `13-Oxa Bicyclo(10.1.0)Trideca- 4,8-Diene, Trimethyl-` | 유지 |
| 3482 / 157 | `7-((3, 7- Dimethyl-2,6- Octadienyl)Oxy)-4-Methyl-2H-1-Benzopyran-2-0ne` → `7-((3,7- Dimethyl-2,6- Octadienyl)Oxy)-4-Methyl-2H-1-Benzopyran-2-One` | 유지 |
| 3493 / 158 | `l-Propanol, 2-Methyl-3-(( 1, 7, 7-Trimethylbicyclo[2.2. l]Hept-2- Yl)Oxy]-` → `1-Propanol, 2-Methyl-3-[(1,7,7-Trimethylbicyclo[2.2.1]Hept-2- Yl)Oxy]-` | 유지 |
| 3501 / 158 | `Prop-2-Yn-1-01; 1-(2,6,6-Trimethyl-1-Cyclohex-2- Enyl)Pentan-3-0ne` → `Prop-2-Yn-1-Ol; 1-(2,6,6-Trimethyl-1-Cyclohex-2- Enyl)Pentan-3-One` | 유지 |
| 3502 / 158 | `1,2,3,4,5,6, 7 ,8,9, 10- Decahydro-5, 9-Methanobenzocycloocten- 11-0ne` → `1,2,3,4,5,6,7,8,9,10- Decahydro-5,9-Methanobenzocycloocten- 11-One` | 유지 |
| 3507 / 159 | `5-Phenyl-4-Hexane-2-0ne` → `5-Phenyl-4-Hexane-2-One` | 유지 |
| 3509 / 159 | `2H- Pyran-2-0ne, Tetrahydro-6-(3-Pentenyl)-` → `2H- Pyran-2-One, Tetrahydro-6-(3-Pentenyl)-` | 유지 |
| 3517 / 159 | `2H-1- Benzopyran-2-0ne,Octahydro-` → `2H-1- Benzopyran-2-One,Octahydro-` | 유지 |
| 3529 / 160 | `2- Cyclohexen-1-0ne,Diethyldimethyl-` → `2- Cyclohexen-1-One,Diethyldimethyl-` | 유지 |
| 3548 / 161 | `lh- Inden-1-0ne, 2,3-Dihydro-2,3,3-Trimethyl-` → `1h- Inden-1-One, 2,3-Dihydro-2,3,3-Trimethyl-` | 유지 |
| 3552 / 161 | `2- Hepten-4-0ne, 5-Methyl-` → `2- Hepten-4-One, 5-Methyl-` | 유지 |
| 3595 / 163 | `1-(5,6, 7 ,8-Tetrahydro-3,5,5,6,8,8, -Hexamethyl-2- Naphthyl)Ethan-1-0ne (AHTN)` → `1-(5,6,7,8-Tetrahydro-3,5,5,6,8,8, -Hexamethyl-2- Naphthyl)Ethan-1-One (AHTN)` | 유지 |
| 3709 / 168 | `Cl73000` → `CI 73000` | 유지 |
| 3710 / 168 | `Cl73015` → `CI 73015` | 유지 |
| 3719 / 168 | `Cl77000` → `CI 77000` | 유지 |
| 3728 / 168 | `Cl77346` → `CI 77346` | 유지 |
| 3729 / 168 | `Cl77400` → `CI 77400` | 유지 |
| 3878 / 175 | `Cl77891` → `CI 77891` | 유지 |

### 공통 런타임 차단 규칙과 추가 제외

`name_exclusion_reasons()`에 `ocr_letter_confusion`을 추가했다. 토큰 끝의 `[oOyY][1I]`와
소문자 사이의 대문자 I를 정규화로 소문자화하기 전에 차단한다. 로더와 입력 해석기가
원래부터 호출하던 공통 함수이므로 둘 다 같은 판정을 사용한다. `name_index`가 없거나
잘못 true인 경우, 이전 프로세스의 색인에 잘못된 이름이 남아 있는 경우도 차단한다.
숫자 위치·탄소 사슬·색소 코드·비타민의 C12, C12-15, 1,2-, -1-yl, CI 77002,
Vitamin B1 및 올바른 -Ol/-One/-ene는 통과하는 테스트를 추가했다.

전체 목록에 새 규칙을 적용하니 **보정표에 없는 기존 이름 9건**도 새로 제외되었다.
이 이름들은 크롭 확인 없는 추정 교정을 하지 않고 원문·CAS·위치를 보존한 채 이름 색인만
제외했다. 아래 9건은 위 136건 재대조 후보 및 재보정 5건과 별개다.

| No. / 쪽 | 보존한 원문 이름 | 처리 |
| --- | --- | --- |
| 575 / 35 | `2-(4-Methylcyclohex-3-En-1-Yl)Propan-2-Y1 Butanoate` | 이름 색인 제외, CAS 보존 |
| 882 / 48 | `Ethyl 2-(2-Methyl-1,3-Dioxolan- 2- YI) Acetate` | 이름 색인 제외, CAS 보존 |
| 888 / 48 | `( lrs,2rs,6rs, 7rs,8sr)-Tricyclo[5.2. l .02,6] Dec-4-En-8- YI Acetate` | 이름 색인 제외, CAS 보존 |
| 889 / 49 | `1-(2,3,8,8-Tetramethyl-1,3,4,5,6, 7-Hexahydronaphthalen-2- YI) Ethan one` | 이름 색인 제외, CAS 보존 |
| 1062 / 56 | `2-Ethyl-4-(2,2,3-Trimethylcyclopent - 3-En-YI)- But-2-En-1-01` | 이름 색인 제외, CAS 보존 |
| 1087 / 58 | `(E)-6, 1 O-Dimethylundeca-5, 9- Dien-2- YI Acetate` | 이름 색인 제외, CAS 보존 |
| 1149 / 60 | `2-Methoxy-1-Methyl-4-Propan- 2-YI benzene` | 이름 색인 제외, CAS 보존 |
| 1620 / 81 | `2-Methoxy-4-Methyl-1-Propan- 2-YI benzene` | 이름 색인 제외, CAS 보존 |
| 2227 / 105 | `2,5-Dimethyl-3-0xo-(2h)-Fur-4- YI Butyrate` | 이름 색인 제외, CAS 보존 |

기존에도 제외되던 No.3272에는 사유만 추가되었다. 보정 전 원문을 복원해 새 규칙을 적용하면
No.2210도 사유 추가 대상이므로 원문 복원 메타데이터의 신규 사유는 총 11건이다.
이 메타데이터 변화는 원문 이름·CAS·위치의 변경을 뜻하지 않는다.

| 집계 | 검수 전 | 검수 후 |
| --- | ---: | ---: |
| 원본 행 수 | 4,112 | 4,112 |
| 적용 보정 / 제외 보정 | 269 / 14 | 269 / 14 |
| 현재 이름 색인 제외 | 168 | 177 |
| 현재 화학물질 이름 연결 | 4,368 | 4,359 |
| 현재 고유 이름 키 | 4,252 | 4,243 |
| 보정 전 원문에 현행 규칙을 적용한 제외 | 437 | 446 |
| 보정 전 원문의 이름 연결 / 고유 키 | 4,104 / 3,990 | 4,095 / 3,981 |
| CAS 연결 / 고유 CAS | 3,905 / 3,591 | 3,905 / 3,591 |

PDF로 다시 생성한 JSON과 현재 저장 JSON은 바이트 단위로 같다.
보정 전 원문에 과거 메타데이터를 복원하면 기존 SHA256
`047d7ef613eb64b54a5c723ae88173c4783624790eee84c27967d0e5ba4c878a`가 유지된다.
새 규칙으로 다시 계산한 원문 복원 SHA256은
`53cb8fe50153a932a18e2d23fc9c6da51cc3b83d5aee48081151ba213b6fb43e`다.

- 현재 JSON SHA256: `af45f905eacd570ac854e795272d9c6e8bc57f9691f66ab9a4d11ab9833c025e`
- 현재 보정표 SHA256: `6a07e13ffdbc3b28b973f637854752a2507e6174e976b7ed5c93853b0c0d99b0`

### 설명문 경계·알림 벨 재현

- 숫자 앞에서 자른 이름뿐 아니라 쉼표로 나눈 항목 전체가 100자를 넘고 8단어를 넘으면
  셀 길이 제한으로 건너뛰기 전에 성분 구간을 닫는다. 검수의 `2 jam` 포함 460자 행 뒤
  Water는 모든 자동 입력 경로에서 확인대기로 남는다. 60성분·1,182자 INCI는 항목별 검사로 유지한다.
- 벨은 관리자 이력과 같은 `NTF_deliveryReasons()`를 호출한다. 채널 결과와 사유를 따로
  한 번씩 표시하여 `last_error: case_closed` 및 채널 접두사가 있는 사유도 KO/EN/ID로 표시한다.
  알 수 없는 공급자 오류는 기존 공통 함수의 일반 문구와 HTML 이스케이프를 따른다.

집중 검증: 372 passed / 4 failed. 실패는 새 제외 메타데이터와 집계를 반영하지 않은
기존 KMA 스냅샷 4개뿐이며, 요청된 새 재현 테스트와 기존 60성분 INCI 테스트는 통과했다.
Node 알림 테스트는 24 passed / 0 failed. 기존 테스트 변경 제한에 따라 스냅샷은 아직 수정하지 않았다.

### 최종 전체 검증과 남은 승인 범위

사용자가 지정한 환경 변수와 명령으로 전체 검증했다.

- Python: **10,749 passed / 4 failed**, 22 warnings, 779.20초.
- Node: **4개 파일, 78 passed / 0 failed**. 요청한 `FAIL` 출력 없음.
- 신규 재현 테스트 52개, 기존 60성분 INCI 및 나머지 기존 기능 회귀는 모두 통과했다.
- `git diff --check`, PDF 재생성 바이트 일치, 원문·CAS·위치 보존, 보정표 283건 크롭 해시·원시 OCR 식별자 검증 통과.
- 기존 테스트 본문·기대값은 작업 시작 시와 동일하다. Node 테스트는 기존 내용 뒤에 12건만 추가했다.

남은 4개 실패는 새 안전 규칙에 따른 이름 색인·메타데이터 변화와 고정 스냅샷의 충돌이다.
기능을 되돌리거나 예외 처리하여 이 값을 맞추지 않았다.

| 파일 / 함수 | 기대값 충돌 | 준비한 변경 |
| --- | --- | --- |
| `test_kma1360_ocr_fixes.py::test_frozen_table_decisions_and_complete_original_provenance` | 원문 복원 시 새 제외 메타데이터도 해시에 포함됨 | 원문 이름·CAS·위치는 유지하고 메타데이터 반영 SHA만 갱신 |
| `test_kma1360_ocr_fixes.py::test_regeneration_and_reapplication_preserve_all_frozen_decisions` | 제외 168 → 177 | 숫자 갱신 |
| `test_kma1360_ocr_fixes.py::test_all_new_names_and_remaining_exclusions_follow_existing_loader_rules` | 현재·원문 복원의 고유 키/연결 수가 각각 9 감소 | 해당 집계 숫자만 갱신 |
| `test_kma1360_p1_round3.py::test_current_catalogue_all_exclusions_aliases_and_cas_are_consistent` | 현재 제외 168 → 177, 원문 제외 437 → 446, 신규 사유 11건 및 복원 SHA | 해당 집계·신규 사유·SHA만 갱신 |

사용자의 '기존 테스트 기대값 변경 금지, 승인한 2함수 숫자만 허용' 범위를 넘으므로
위 변경은 **승인 대기·미적용**이다. 따라서 현재 **실패 0 조건은 아직 충족하지 못했다**.
검토 가능한 패치는 `/private/tmp/polish-fix1-snapshot-update-proposed.patch`에 준비했다.
전체 로그는 `/private/tmp/polish-fix1-pytest-full.log`, Node 로그는
`/private/tmp/polish-fix1-node-all.log`다. 운영 접속·배포·커밋·브랜치 변경은 하지 않았다.

## 검수 2차 반려 후 병합 보정·설명문 재감사 (2026-11-15 KST)

검수 전문 `polish_review2_out.md`의 P1·P2를 재현했다. cli-v2는 Port 1에서
`api.openai.com` 연결 실패로 REJECT했으므로 사용자 지침의 실패 시 fallback으로 수정했다.
기존 미커밋 변경은 `/private/tmp/polish-review3.wZXoF7/preexisting-files.tar.gz`와
`preexisting.diff`에 백업했다. 운영 접속·배포·커밋은 하지 않았다.

### 보정표 283건 전체의 병합 감사

전체 보정 후보의 원래값·보정값·CAS 개수·크롭 크기·SHA256·원시 OCR 좌표를 확인했다.
283개 크롭 모두 보정표의 크기와 해시가 일치한다. 이름 영역 내부에서 크롭 너비의 65%를
넘는 연속 수평선을 찾고, OCR 줄 좌표·줄 사이 간격·전체 크롭 높이와 대조했다.
수평선 후보는 b/5, b/9, c.2/475였으며 세 크롭을 직접 열어 서로 다른 이름 칸임을 확인했다.
No.475는 원본 PDF 31쪽 전체에서도 같은 경계를 확인했다.

복수 CAS는 8건이며, 원래 제외된 4건도 `merged_cells` 사유를 추가했다.
보정값 전체의 화학명 재시작 패턴과 세미콜론도 대조하여 No.3501을 추가 제외했다.
No.3006의 높은 수평 잉크 비율은 글자 획이었다. 크롭과 연속선 검사로 구분선이 아님을
확인했으며, 정상 한 이름의 줄바꿈을 병합으로 판정하지 않았다.
283건 모두의 이미지를 사람이 직접 읽었다는 뜻은 아니다.

| 구분 / No. | PDF 쪽 | 근거 | 처리 |
| --- | ---: | --- | --- |
| b / 5 | 8 | 1823×744, 셀룰로스 계열 7칸; 내부 선 y≈124/234/343/451/559/665 | 적용 → 제외, `merged_cells` |
| b / 9 | 8–9 | 1838×1195, 페이지별 OCR 좌표를 가진 연속 크롭; 폴리머 이름 칸 사이 수평선 | 적용 → 제외, `merged_cells` |
| c.2 / 475 | 31 | 1186×273, 첫 이름 y=19–136·둘째 y=201–255, 구분선 y=194–197; CAS 2개 | 적용 → 제외, `merged_cells` |
| c.2 / 3501 | 158 | 1171×154, `Prop-2-Yn-1-Ol;` 뒤 `1-(2,6,6-Trimethyl-…)Pentan-3-One`; 단일 CAS와 각 이름 대응 미확인 | 적용 → 제외, `merged_cells` |
| c.2 / 3610 | 164 | CAS 3개, 세 염료 혼합 설명과 세 화학명 | 적용 → 제외, `merged_cells` |
| c.2 / 3869 | 174 | CAS 5개, 기존 다른 CAS 충돌 | 제외 유지, `merged_cells` 추가 |
| c.2 / 3872 | 174 | CAS 3개, 기존 다른 CAS 충돌 | 제외 유지, `merged_cells` 추가 |
| c.2 / 3873 | 174 | CAS 5개 | 적용 → 제외, `merged_cells` |
| c.2 / 3874 | 175 | CAS 3개, 기존 다른 CAS 충돌 | 제외 유지, `merged_cells` 추가 |
| c.2 / 3875 | 175 | CAS 6개 | 적용 → 제외, `merged_cells` |
| c.2 / 3876 | 175 | CAS 2개, 기존 다른 CAS 충돌 | 제외 유지, `merged_cells` 추가 |

총 11건에 `merged_cells`가 기록되며, 이 중 적용 상태였던 7건만 원문으로 되돌렸다.
보정 후보와 크롭·OCR 근거는 표에 보존한다. 이름과 CAS를 추정하여 분할하지 않았고,
11건의 원문 이름은 모두 색인 제외 상태다. 개별 유효 CAS의 기존 색인 정책은 유지했다.

### 이름 색인·보정 재생성의 방어

`has_merged_chemical_names()`는 완결된 `Ester` 또는 위치번호가 있는 화학명 말미
(`-1-Ol`, `-4-One` 등) 뒤에 공백·세미콜론을 거쳐 새 숫자 위치표기 이름이 시작하는지 검사한다.
이미 제외된 원문은 기존 제외 사유를 보존하고, 이외에는 `merged_chemical_names`로 제외한다.
런타임 로더와 입력 검사에 공통 적용되므로 없거나 오래된 `name_index`도 통과 근거가 되지 않는다.
재생성기는 복수 CAS 또는 병합 형태의 보정을 `applied`로 새로 제출하면 행 변경 전에 거부한다.

- No.475 병합 문자열, `(CAS 13058-12-3)` 추가, `(CAS 95-41-0)` 추가 모두
  사전 매칭 없는 조건에서 KMA 매칭 없음, `NEEDS_EVIDENCE`, 자동등록 불가다.
- `2,4-Dimethyl-2-(5,5,8,8-Tetramethyl-6,7-Dihydronaphthalen-2-Yl) -1,3-Dioxolane`은
  정상 이름으로 유지되며, `-Yl 3-Methylbutanoate`와 alkyl/위치표기 조합도 새 규칙에 걸리지 않는다.
- `Propanoic Acid, 2-Methyl-, 2,2-Dimethyl-1-(1-Methylethyl)-1,3- Propanediyl Ester`도
  병합으로 보지 않는다. 이 문자열의 `-,`에 대한 기존 `repeated_special_characters` 판정은
  이번 작업에서 완화하지 않았으며, 병합 검사 자체의 오탐 방지와 별도로 고정했다.

### 설명문 판정과 자동등록 재현

설명문 단어 수·길이에는 일반 단어만 사용한다. 표 구분자(`|`, 전각 `｜`, 탭), CAS 표지와 번호,
함량·단위(`%`, `w/w` 등), 숫자·위치표기 토큰은 제외한다. 숫자 앞에서 잘린 짧은 이름만
보지 않고 항목 전체의 일반 단어도 검사하므로, 긴 설명문을 셀 상한 때문에 건너뛰어
뒤의 Water를 성분으로 오인하는 문제도 계속 막는다.

검수 입력의 `Salt 10%`, 긴 화학명 `| CAS 131812-67-4 | 10 % w/w`, `Water | 90%`는
세 성분 모두 추출된다. 실제 사전을 사용하는 `aggregate_fields()` 및 집계·파일 파싱·재처리·
ZIP·스트리밍 ZIP 자동 입력 경로에서 세 성분 저장과 제품 연결을 검증했다.
기존 마침표 없는 인니어 설명문 492자와 숫자 포함 460자 뒤 Water는 계속 확인대기다.

### 집계·원문 보존·검증

| 집계 | 작업 시작 | 이번 결과 |
| --- | ---: | ---: |
| 보정표 행 수 | 283 | 283 |
| 적용 / 제외 보정 | 269 / 14 | 262 / 21 |
| `merged_cells` 사유 | 0 | 11 |
| 현재 이름 색인 제외(a/b 포함) | 177 | 184 |
| 화학물질 고유 이름 / 이름 연결 | 4,243 / 4,359 | 4,238 / 4,354 |
| 보정 전 원문 제외 | 446 | 446 |
| 고유 CAS / CAS 연결 | 3,591 / 3,905 | 3,591 / 3,905 |

기존 테스트는 KMA 집계 스냅샷 2개 파일의 해당 숫자와 신규 사유 집계만 갱신했다.
기존 단언·검증 경로·원문 고정 해시는 그대로다. 새 회귀 테스트는
`tests/test_polish_review2_regressions.py`에 추가했다.

- 원문 6필드 SHA256: `dc46e13950261e097e9673e5c9a8ff5119ade72639e455033126e01d117cb5c6` (유지).
- 기존 제외 메타데이터를 포함한 원문 복원 SHA256: `53cb8fe50153a932a18e2d23fc9c6da51cc3b83d5aee48081151ba213b6fb43e` (유지).
- 현재 JSON SHA256: `f13f0533870de6cb2f21aadbe3ab72acadf3a1151a9bc2e6523d687c58faa16f`.
- 현재 보정표 SHA256: `d9fc08e1102eb8ec53eca72d458ccba25452fe39f9ed333094472129da4d55d5`.
- 집중 검증: **459 passed / 0 failed**. 기존 자동등록·설명문 회귀도 통과했다.
- Node: 요청한 루프의 **4개 파일 모두 성공**, `FAIL` 출력 없음.
- 전체 pytest: **10,779 passed / 0 failed**, 22 warnings, 784.77초.
  `GLHAC_DEV=1 GLHAC_NOTIFY_WORKER=0 GLHAC_RATE_LIMIT_PER_MIN=0 .venv/bin/python -m pytest -q -p no:cacheprovider tests/`
  명령으로 완료했다. 로그: `/private/tmp/polish-review3.wZXoF7/pytest-full.log`.
- PDF 재생성 바이트 일치와 `git diff --check` 통과. 기존 KMA 테스트 두 파일의 단언 수는
  각각 62개·51개로 작업 전후 동일하며, 이외 기존 테스트 파일은 변경하지 않았다.

Node 로그는 `/private/tmp/polish-review3.wZXoF7/node-all.log`, 크롭 전수 감사 결과는
같은 디렉터리의 `full-table-audit.json`에 보관한다. 요청한 실패 0 조건을 충족했다.
