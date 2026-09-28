# Anomaly Interpretation — 통계 판정 기준 메모

> 이 파일은 `anomaly_engine.analyze_commonality`의 통계 판정에 대한
> 해석 관점(PCHK 측정이상 추정·공간 패턴 라벨)을 정리한 참고 문서입니다.
> 자동 판정 규칙([RULE]/NL_RULES)은 사용하지 않는다(순수 통계만).
> 수치 임계값(σ·배수 등)은 `My_config.py`에서 관리한다.

---

## 측정이상 추정 관점 (참고)

> 코드는 사실(어느 shot에서 무엇이 spec-out인지)만 산출하고,
> "측정이상으로 볼지"의 판정 관점은 아래를 따른다.

### PCHK 동일 shot spec-out → 측정이상 추정

- **PCHK LKG**(프로브 누설전류 체크)가 **동일 PGM(pt)에서 동일 shot(같은 CHIP_X/Y·wafer)** 에
  spec-out이면, 그 site의 측정값은 **측정이상**(프로브 접촉 불량/누설 경로)으로 **추정**한다.
  → 실제 소자 불량으로 단정하기 전에 **재측정으로 재현성 확인**을 우선 권고한다.
- **동일 shot에서 해당 PCHK의 검증 대상 측정Item이 함께 spec-out**이면 측정이상 가능성이 **더 높다.**
  즉 **여러 측정Item이 동일하게 spec-out될수록 측정이상 확신도가 올라간다.**
- 반대로 **PCHK는 정상인데 측정Item만 spec-out**이면 측정이상보다는 **실제 공정/소자 불량** 쪽에 무게를 둔다.
- 코드는 이 신호를 finding `type=MEAS_SUSPECT`(신호등 🟡 "측정이상 추정")로 표기하고,
  겹친 항목·shot 수·좌표·PGM(pt)를 상세에 담는다.

### PCHK 종류별 '검증 대상 CAT2' 매핑

> **PCHK 종류별로 검증하는 CAT2(카테고리) 군을 '따로' 관리한다.** 누설 체크(**PCHK_LKG**)는
> 누설에 민감한 카테고리를, 접촉저항 체크(**PCHK_RES**)는 저항/구동 카테고리를 각각 검증한다.
> 매핑에 적은 **CAT2에 속한 항목 전체**가 PCHK와 동일 PGM(pt)·동일 shot에서 함께 spec-out일 때만
> 그 항목들을 측정이상으로 본다.
>
> - 형식: `- <PCHK 표시명>: CAT2_1, CAT2_2, ...` (값은 **개별 항목명이 아니라 CAT2 이름**).
> - **CAT2·항목명은 원 이름이든 HTML/PPT 표시명이든 둘 다 인식**한다.
> - 매핑에 없는 PCHK는 (하위호환) 모든 spec-out 항목과 대조한다.

**아래 두 마커(`PCHK_ITEM_MAP`) 사이의 줄만 실제로 반영된다. (`CAT2_A` 등은 실제 CAT2 이름으로 교체)**

<!-- PCHK_ITEM_MAP:start -->
- PCHK_LKG: CAT2_A, CAT2_B
- PCHK_RES: CAT2_C, CAT2_D
<!-- PCHK_ITEM_MAP:end -->

## 특이맵(공간 패턴) 라벨 해석 (참고 — 코드가 판정)

> spec-out 좌표의 공간 패턴은 **코드(`classify_specout_pattern`)가 판정**해 finding의
> `spec_out_pattern`에 라벨로 담는다.
>
> 라벨 종류: `전면성` · `세로/가로 줄성(x=... / y=...)` · `Center 집중` · `Edge ring` ·
> `Middle 환형` · `k시 방향 클러스터` · `우상/좌상/좌하/우하 사분면` · `상/하반구`,
> `좌/우측 반면` · `산발(특정 패턴 없음)` · `소수 pt`(판정 보류).
> 참고 해석(일반론): 줄성→스캐너/프로브카드 열, Edge ring→엣지 공정(베벨/링), Center→중심
> 균일도, k시 방향→노치 기준 국소 클러스터. 단정하지 말고 확인 포인트로만.
