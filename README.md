# HOL Auto Report System

> 반도체 HOL(Head-Of-Line) DC 측정 결과 **자동 분석 · 리포팅 시스템**
> ET 측정 데이터 → 통계 자동 해석 → PPT/HTML 리포트 → 메일 발송까지 한 번에.

---

## 📋 목차
- [개요](#개요)
- [저장소 구성 (setup.py 번들)](#저장소-구성-setuppy-번들)
- [빠른 시작](#빠른-시작)
- [전체 사용법 (설치 · 설정 · 실행 · 출력물)](#전체-사용법-설치--설정--실행--출력물)
- [아키텍처](#아키텍처)
- [불량 통계 자동 분석 (핵심)](#불량-통계-자동-분석-핵심)
  - [analyze_commonality — 코드 단독 동작](#analyze_commonality--코드-단독-동작)
  - [지표(metrics) 계산식](#지표metrics-계산식)
  - [신호등 3색 등급](#신호등-3색-등급)
  - [Anomaly Trend Chart & spec-out WF MAP](#anomaly-trend-chart--spec-out-wf-map)
- [AI 다단계 해석 (선택)](#ai-다단계-해석-선택)
  - [ANOMALY_KNOWLEDGE.md — 페르소나·답변 스타일](#anomaly_knowledgemd--페르소나답변-스타일)
  - [LLM 연결 / 토글](#llm-연결--토글)
- [설정 가이드 (My_config.py)](#설정-가이드-my_configpy)
- [리포트 구성 요소](#리포트-구성-요소)
  - [Score Board / WF MAP](#score-board--wf-map)
  - [PPT 차트 / 다중 lot](#ppt-차트--다중-lot)
  - [ADDP 파생 항목 (Reformatize)](#addp-파생-항목-reformatize)
- [로직 확장 가능성 · 분석 로직 카탈로그](#로직-확장-가능성--분석-로직-카탈로그)
  - [확장 포인트 (어디에 로직을 추가하나)](#확장-포인트-어디에-로직을-추가하나)
  - [넣을 수 있는 분석 로직 (아이디어)](#넣을-수-있는-분석-로직-아이디어)
  - [불량 모드 판정표 (참고 지식)](#불량-모드-판정표-참고-지식)
  - [통계 패턴 → 추정 원인 카탈로그](#통계-패턴--추정-원인-카탈로그)
- [주요 함수](#주요-함수)
- [데이터 흐름](#데이터-흐름)

---

## 개요

Python 기반 반도체 HOL DC 측정 데이터 자동 분석 · 리포트 생성 시스템입니다.

- **ET(Electrical Test) 데이터 쿼리** → Hive 파티셔닝 Parquet 저장
- **DuckDB** 기반 고속 인메모리 처리 (Scale Factor / ADDP 파생 연산 / 피벗)
- **Pass Rate Score Board** + wafer별 WF MAP
- **불량 통계 자동 분석**: 코드만으로 동작하는 1차 이상 해석(Finding) — AI 없이도 항상 결과 산출
- **AI 다단계 해석(선택)**: 통계 Finding 위에 자연어 원인/조치 보강, 불량 모드 판정
- **PPT / HTML 리포트** 자동 생성 및 메일 발송

설계 목표 두 가지:
1. **AI-optional** — GPT가 있으면 서술이 풍부해지지만, **없거나 실패해도 코드만으로 동일한 통계 분석**이 나온다.
2. **준실시간** — 한 lot 리포트를 끝까지 처리하는 데 목표 20분 이내(현 데모 약 1분).

---

## 저장소 구성 (setup.py 번들)

이 저장소에는 **5개 파일만** 올라갑니다.

| 파일 | 역할 |
|------|------|
| `setup.py` | **자가추출 번들**. 전체 소스(아래 6개 파일)를 gzip+base64로 임베드. 실행하면 풀린다 |
| `Main.py` | 메인 파이프라인 (진입점) |
| `My_Function.py` | 유틸리티 + 데이터 쿼리 + PPT/차트 생성 |
| `My_config.py` | 글로벌 설정 + HTML 템플릿 내장(`_REPORT_HTML_TEMPLATE`) |
| `README.md` | 본 문서 |

### setup.py가 풀어내는 파일

`setup.py`는 위 3개 코드 파일 + **저장소에는 별도로 노출되지 않는** 다음 파일까지 모두 포함합니다.

| 추출 파일 | 역할 |
|------|------|
| `anomaly_engine.py` | **불량 통계 자동 분석 엔진** (`analyze_commonality`, `interpret_with_ai`, `render_findings_html`) |
| `ANOMALY_KNOWLEDGE.md` | AI 해석 **페르소나·답변 스타일/톤/형식** 가이드 (분석 로직·불량 모드 규칙은 넣지 않음 → 본 README [로직 확장](#로직-확장-가능성--분석-로직-카탈로그) 참조) |

```bash
python setup.py            # 현재 폴더에 추출 (이미 있는 파일은 skip)
python setup.py --overwrite  # 기존 파일 덮어쓰기
python setup.py --list       # 번들 내용만 확인 (추출 안 함)
python setup.py -o DIR       # DIR 폴더에 추출
```

> setup.py는 추출 시 **SHA-256 체크섬**으로 내용 무결성을 검증합니다.

### 사내 이식 시 (중요)

- **코어 코드는 `Main.py` / `My_config.py` / `My_Function.py` / `anomaly_engine.py` 4개 + `ANOMALY_KNOWLEDGE.md`** 뿐입니다.
- `bigdataquery`(DB 쿼리), `gpt_oss_client`(LLM 클라이언트)는 **로컬 테스트용 mock**이며, **사내 환경에는 실제 모듈이 이미 존재**하므로 번들에 포함하지 않습니다.
  - `Main.py`는 `gpt_oss_client`를 `try/except`로 감싸 없으면 AI 해석만 비활성화하고 정상 동작합니다.
  - `bigdataquery`는 `Main.py`/`My_Function.py`가 직접 import 하므로, 사내든 로컬이든 **해당 모듈이 경로에 있어야** 합니다.
- 과거 `templates/report.html`로 분리돼 있던 HTML 템플릿은 추가 파일이 따라가지 않도록 `My_config.py`의 `_REPORT_HTML_TEMPLATE` 상수로 **내장**했습니다.

---

## 빠른 시작

```bash
# 1) 소스 추출
python setup.py

# 2) 기본 실행 (vehicle_A)
python Main.py vehicle_A

# 3) 강제 발행 (특정 LOT 즉시 리포트)
python Main.py _TRIGGER_vehicle_A_T6677.1_test
#                          └vehicle┘ └lot┘ └step┘
```

새 vehicle 추가:
1. `reformatter/config.yaml`에 vehicle 블록 추가
2. `reformatter/<vehicle>_reformatter.csv` 작성 (항목 정의: REAL / ADDP)
3. `python Main.py <vehicle>`

---

## 전체 사용법 (설치 · 설정 · 실행 · 출력물)

### 1) 요구사항

- **Python 3.9+**
- **패키지**: `pandas`, `numpy`, `duckdb`, `pyarrow`, `python-pptx`, `matplotlib`, `openpyxl`, `requests`, `pyyaml`, `openai`(AI 사용 시), `Pillow`
- **사내 전용 모듈**(번들 미포함, 경로에 있어야 함): `bigdataquery`(ET/inline/WIP 쿼리). `gpt_oss_client`는 로컬 mock이며 없으면 AI만 비활성.
- 설명 슬라이드 삽입은 이제 **python-pptx로 직접 복사** — PowerPoint(win32com) 설치 불필요.

### 2) 설치 (소스 추출)

```bash
python setup.py              # 현재 폴더에 전체 소스 추출 (이미 있으면 skip)
python setup.py --overwrite  # 덮어쓰기
python setup.py --list       # 번들 내용만 확인
python setup.py -o DIR       # DIR에 추출
```
→ `Main.py / My_Function.py / My_config.py / anomaly_engine.py / ANOMALY_KNOWLEDGE.md`가 풀립니다(SHA-256 검증).

### 3) 필요 파일 (실행 전 준비)

| 파일/폴더 | 필수 | 역할 |
|---|---|---|
| `reformatter/config.yaml` | ✅ | vehicle별 설정(쿼리 파라미터·기간·토글 등) |
| `reformatter/<vehicle>_reformatter.csv` | ✅ | 항목 정의(REAL/ADDP), SPEC·`REPORT DIRECTION`·`SCALE FACTOR`·`CAT1/CAT2`·`PPT_ONLY` 등 |
| `.env` | AI/메일 시 | `GPT_API_BASE_URL`, `GPT_CREDENTIAL_KEY`(AI), 메일/S3 자격 등 |
| `HOL_Auto_Report_Description.pptx` | 선택 | CAT2별 설명 간지. 있으면 해당 슬라이드를 리포트에 직접 복사 삽입(없어도 진행) |
| 좌표 xlsx(zone define) | 선택 | WF MAP 실좌표(flat-zone) 보정용 |
| `ANOMALY_KNOWLEDGE.md` | 선택 | AI 페르소나·답변 스타일(AI 사용 시) |

> `reformatter/`, `config.yaml`, `*.pptx/xlsx/png`, `.env`, mock 모듈은 **setup.py 번들에서 제외**됩니다(사내 별도 관리).

### 4) 설정 (My_config.py)

- **AI 토글**: `use_gpt_summary`(마스터), `use_gpt_multistep`(3단계).
- **분석 임계값**: `anomaly_lot_median_sigma`, `anomaly_lot_dispersion_ratio` 등.
- **WF MAP**: `scoreboard_wfmap_min_pts`(=50), `wfmap_exclude_keywords`(예: `['PCHK']`).
- **이미지 해상도**: PPT `ppt_chart_dpi`/`ppt_map_jpg_quality`, HTML `html_chart_dpi`/`html_wfmap_dpi`(독립 조정).
- **색상**: `score_color_scale`(Score Board 연속 색), `score_color_scale_by_item`(ITEM별 override).
- 자세한 표는 [설정 가이드](#설정-가이드-my_configpy) 참조.

### 5) 실행

```bash
python Main.py <vehicle>                       # 예약/조건에 따라 대상 lot 자동 리포트
python Main.py _TRIGGER_<vehicle>_<lot>_<step> # 특정 LOT 즉시 강제 발행
#              예) _TRIGGER_vehicle_A_T6677.1_test
```

### 6) AI 사용 설정 (선택)

1. `.env`에 `GPT_API_BASE_URL`, `GPT_CREDENTIAL_KEY` 설정.
2. `My_config.use_gpt_summary=True`, `use_gpt_multistep=True`.
3. 사내 환경은 `Main._build_llm_fn`이 자동으로 `gpt_client`(gpt-oss-120b) 사용. 로컬은 `gpt_oss_client.mock_llm`.
4. `.env`나 연결이 없으면 **AI만 비활성**되고 코드 통계 분석으로 정상 리포트 생성.
   (자세한 호출 흐름은 [AI 다단계 해석](#ai-다단계-해석-선택) 참조.)

### 7) 출력물

| 경로 | 내용 |
|---|---|
| `RUN/Report/<vehicle>/HTML/<날짜>-<prod>-<lot>-HOL_<step>_Report_v13.html` | HTML 리포트([0] 요약·Score Board·Inline·상세) |
| `RUN/Report/<vehicle>/Mail/<...>.pptx` | 메일용 PPT (표지·Score Board·항목별 차트·Index Aggregation·Anomaly 상세) |
| `RUN/TEMP/<alias>.png` | HTML이 재참조하는 Trend PNG(해상도 `html_chart_dpi`) |
| `RUN/TEMP/anomaly_basis_<lot>.json/.csv` | Anomaly 판단 근거(spec-out wafer·robust 산포·이탈도) |
| `RUN/DB/<vehicle>_daily/date=YYYY-MM-DD/data.parquet` | Hive 파티션 ET 데이터 |
| `RUN/log/` | 실행 로그 |

---

## 아키텍처

### 설정 3원화

| 파일 | 역할 | 편집 주체 |
|------|------|----------|
| `My_config.py` | 전체 공통 설정 (경로, 임계값, 색상, 토글) | 시스템 관리자 |
| `reformatter/config.yaml` + `*_reformatter.csv` | Vehicle별 항목·스펙·ADDP 정의 | Vehicle 담당자 |
| `ANOMALY_KNOWLEDGE.md` | AI 해석 **페르소나·답변 스타일/톤/형식** | 분석 설계자 |

**핵심 원칙**: 코드를 고치지 않고 위 3개 파일만 편집해서 동작을 바꾼다.

### 런타임 디렉토리(추출/실행 후 생성)

```
auto report/
├── reformatter/
│   ├── config.yaml                  # vehicle별 설정
│   └── vehicle_A_reformatter.csv    # 항목 정의 (REAL/ADDP/PCHK)
└── RUN/
    ├── DB/vehicle_A_daily/date=YYYY-MM-DD/data.parquet   # Hive 파티션 ET 데이터
    ├── TEMP/<alias>.png                                  # HTML이 재참조하는 Trend PNG
    ├── Report/vehicle_A/{HTML,Mail}/                     # 산출물
    └── log/                                              # 실행 로그
```

---

## 불량 통계 자동 분석 (핵심)

리포트 최상단 **`■ [0] Anomaly Summary`** 섹션의 핵심 엔진입니다.
**AI(GPT) 사용 여부와 무관하게 항상 코드로 동작**하며, AI가 켜져 있으면 그 위에 자연어 해석을 얹습니다.

[0] 섹션은 위에서부터 다음 순서로 조립됩니다(`Main.py`):

1. **(AI 있으면) AI 다단계 해석** — 연한 색 박스, "AI 자동 생성 참고용" 안내
2. **통계 기반 자동 분석** — `render_findings_html` (HTML엔 **상위 5건만**, 전체 N건은 PPT 상세 참조)
3. **Anomaly Trend Chart** — 이상/주의 항목의 Trend 차트 + spec-out WF MAP 그리드

> 같은 통계 Finding이 PPT에서는 Score Board **바로 뒤** `Anomaly 상세(통계)` 페이지(`insert_findings_page`)에 **전체**가 들어갑니다.

### analyze_commonality — 코드 단독 동작

`anomaly_engine.analyze_commonality()`는 **각 측정 Index(항목)마다 '한 개'의 Finding**만 산출합니다.
판정 우선순위는 다음과 같으며, 위에서 매칭되면 그 항목은 더 보지 않습니다.

| 우선 | type | severity | 조건 | 코멘트 |
|---|---|---|---|---|
| 1 | `SPEC_OUT` | 🔴 CRITICAL (이상) | 타깃 lot에서 spec(SPECLOW~SPECHIGH) 이탈 측정점 ≥ 1 | 이탈 개수 + 최대 이탈값 + target/모집단 median |
| 2 | `MEDIAN_SHIFT` | 🟠 WARNING (주의) | spec 미초과 + `deviation > anomaly_lot_median_sigma` (산포보다 두드러질 때) | "spec 내이나 분포 이동", target/모집단 median |
| 3 | `DISPERSION` | 🟠 WARNING (주의) | spec 미초과 + `std_ratio > anomaly_lot_dispersion_ratio` | "lot내 산포 증가(균일도 검토)", target/모집단 std |

- 정렬: `(severity, -(spec_out_count*1000 + deviation))` → 이상 항목이 먼저, 그 안에서 이탈이 큰 순.
- **각 detector는 항목 단위 try/except**라 한 항목이 실패해도 나머지 분석은 계속됩니다.
- **spec-out 분류는 리포팅 대상 `target_lot_id`에만 한정**합니다. 같은 root의 형제 lot은 이상으로 분류하지 않습니다.
- spec-out 판정은 reformatter의 `REPORT DIRECTION`(UPPER/LOWER/BOTH)을 따릅니다.
  예) `LOWER` 항목은 상한 초과를 불량으로 보지 않으므로, 상한을 넘어도 spec-out이 아니라 *median 이탈(🟠)* 로 잡힐 수 있습니다.

> **불량 모드(여러 Index 조합 해석)는 코드가 하지 않습니다.**
> 코드는 위 단일 이상만 산출하고, 조합→불량모드 판정은 **AI 연결 시에만** `ANOMALY_KNOWLEDGE.md`의 '불량 모드 판정표'로 수행합니다([AI 다단계 해석](#ai-다단계-해석-선택) 참고).

### 지표(metrics) 계산식

판정에 쓰는 항목별 통계는 `My_Function.insert_plots()`가 차트를 그리며 함께 계산해 `metrics_dict`로 넘깁니다.

| 지표 | 정의 |
|---|---|
| `global_med` / `global_std` | **모집단**(main vehicle 전체) median / std |
| `target_med` / `target_std` | **타깃 lot** median / std |
| `deviation` | `|target_med − global_med| / global_std` — 타깃 median이 모집단에서 몇 σ 떨어졌나 |
| `std_ratio` | `target_std / global_std` — 타깃 산포가 모집단 대비 몇 배인가 (engine 내부 계산) |
| `spec_outs` / `spec_out_count` | 타깃 lot의 spec 이탈 측정점 목록(값·좌표) / 개수 |

### 신호등 3색 등급

각 Finding은 **신호등 원(●) + 라벨**로 표시됩니다(HTML 요약·head, PPT 상세 모두 동일 색).

| 신호등 | severity | 라벨 | 요약 head 표기 | 현재 산출 detector |
|---|---|---|---|---|
| 🔴 빨강 | `CRITICAL` | 이상 | 이상 | `SPEC_OUT` |
| 🟠 주황 | `WARNING` | 주의 | 주의 | `MEDIAN_SHIFT`, `DISPERSION` |
| 🟡 노랑 | `NOTICE` | 주의 | 측정이상 추정 | (현재 코드 detector 없음 — 요약 범례용으로 유지) |
| ⚫ 회색 | `INFO` | 참고 | 참고 | — |

- 색/라벨은 `anomaly_engine.py`의 **`_SEV_COLOR` / `_SEV_LABEL` / `_SEV_HEAD` 한 곳**에서 관리합니다.
  PPT 상세 페이지는 `My_Function.insert_findings_page`가 같은 색을 미러링합니다.
- 어떤 Finding을 어느 등급으로 둘지는 그 detector의 `_finding("CRITICAL"|"WARNING"|"NOTICE", ...)` 첫 인자로 결정됩니다.
- **코멘트(제목/상세 문구)는 detector 코드가 직접 생성**합니다. 문구를 바꾸려면 `analyze_commonality()` 안 `_finding(..., title=..., detail=...)` 문자열을, 민감도는 `My_config.py` 임계값을 수정합니다.

### Anomaly Trend Chart & spec-out WF MAP

[0] 섹션의 Trend 차트 그리드는 **통계 자동 분석(`code_findings`) 상위와 동일한 항목**을 사용합니다(콤마 분해·중복 제거 후 `merged_df` 컬럼 + Trend PNG가 있는 것만, 최대 `anomaly_trend_chart_top_n`개).

레이아웃은 두 그룹:

- **이상 탭** (`● 이상`): `SPEC_OUT` 항목을 **1행씩** — 좌측 Trend 차트 + **우측 spec-out WF MAP**.
- **주의 탭** (`● 주의`): 나머지를 가로 flex-wrap 배치(WF MAP 없음).

**spec-out WF MAP**(`render_specout_wfmaps_b64`):
- 칩 색: 통과=회색(`#bdbdbd`), spec 이탈(flier)=빨강(`#d32f2f`).
- **타깃 lot의 spec-out wafer는 전량 우선 표시**(25매 모두 spec이면 25장). 남는 칸만 다른 lot을 TKOUT_TIME 최신순으로.
- 상한은 `anomaly_wfmap_max_count`(=25)지만 **타깃 spec wafer는 상한과 무관하게 모두** 표시.
- spec_low/high는 `REPORT DIRECTION`을 반영해 전달 → Trend의 SPEC OUT 판정과 일치.

**상태 스티커**: 각 Trend 차트 좌상단에 CSS 배지 — `SPEC OUT`(빨강) / `WARNING`(주황). PNG 자체는 수정하지 않으므로 PPT 차트에는 영향 없음(HTML [0]에만 적용).

---

## AI 다단계 해석 (선택)

> **한 줄 요약**: AI는 **1회 호출이 아니라 3단계(triage→root-cause→final) 순차 호출**로 사용됩니다.
> 입력은 **코드가 계산한 통계 Finding + `ANOMALY_KNOWLEDGE.md`(페르소나/스타일) 텍스트**뿐이며,
> **원측정 raw 데이터/reformatter는 AI에 넘기지 않습니다.** 출력은 [0] 섹션 상단에 붙는 HTML `<ul>` 참고 요약입니다.
> AI가 없거나 어느 단계든 실패하면 **None → [0] 섹션엔 코드 통계 분석만** 표시됩니다(AI-optional).

### 1) AI 연결 셋업 (프로그램 시작 시 1회 — `Main.py`)

`Main.py` 상단에서 **import 시점에 딱 한 번** LLM transport를 구성합니다.

- `.env`에서 `GPT_API_BASE_URL`, `GPT_CREDENTIAL_KEY`를 읽습니다.
- 둘 다 있으면 OpenAI 호환 클라이언트 생성 후 **연결 테스트("Hi" 전송)** → 성공 시 `GPT_CONNECT=True`.
  ```python
  gpt_client = OpenAI(api_key="dummy", base_url=GPT_API_BASE_URL,
                      default_headers={"x-dep-ticket": GPT_CREDENTIAL_KEY, ...})  # model: gpt-oss-120b
  ```
- `_build_llm_fn()`이 **transport 함수**를 반환합니다 — 이 함수가 실제 LLM 호출의 유일한 창구:
  - 연결됨 → `gpt_client.chat.completions.create(model="gpt-oss-120b", messages=[{system},{user}], temperature=0.3)`의 응답 텍스트 반환.
  - 미연결 → `gpt_oss_client.mock_llm`(로컬 mock)이 있으면 사용, 없으면 `None`.
- `_ANOMALY_KNOWLEDGE_TEXT` = `ANOMALY_KNOWLEDGE.md` 전체 텍스트(`My_config.anomaly_knowledge_path`)를 미리 읽어 둡니다.

즉 실제 LLM API는 `_LLM_FN(system, user) -> str` **한 개 시그니처**로 추상화되어, 사내 gpt_client든 로컬 mock이든 동일 코드로 동작합니다.

### 2) AI 호출 조건 (리포트마다 — `Main.py` [0] 섹션)

lot 리포트 생성 루프에서 아래를 **모두** 만족할 때만 AI를 호출합니다.

```python
if GLOBAL_CONFIG.use_gpt_summary and GLOBAL_CONFIG.use_gpt_multistep \
   and code_findings and _LLM_FN is not None:
    ai_html = interpret_with_ai(code_findings, metrics_dict,
                                _ANOMALY_KNOWLEDGE_TEXT, _LLM_FN,
                                config=GLOBAL_CONFIG, target_lot_id=target_lot_id)
```

- `code_findings` = `analyze_commonality()`가 **AI와 무관하게 이미 산출**한 통계 Finding 리스트.
- 하나라도 조건 불충족(토글 off / Finding 없음 / LLM 없음)이면 AI 호출 자체를 건너뜁니다.

### 3) `interpret_with_ai` — 3단계 순차 호출 (`anomaly_engine.py`)

각 단계가 `_LLM_FN(system, user)`를 **한 번씩 호출**하고, 앞 단계 출력이 다음 단계 입력이 됩니다(총 **LLM 3회 호출**).

| 단계 | system 프롬프트 | user 입력 | 출력 | 비고 |
|---|---|---|---|---|
| ① **Triage** | "현상(phenomenon) 단위로 3~6개로 묶어라" | `target_lot` + **findings JSON**(severity/type/item/title/detail) | 현상 정리 텍스트 | 파생항목 통합, 측정 의심 최상단 |
| ② **Root-cause** | "**[지식베이스]** 근거로 추정 원인·확인 포인트" + `ANOMALY_KNOWLEDGE.md` 텍스트 | ① 결과 | 추정 원인 텍스트 | 지식 텍스트가 여기서 주입됨 |
| ③ **Final** | "종합 판단 + 불량 모드 판정, HTML `<ul>`로만 출력" + 지식 텍스트 | spec-out Index 조합 + ① + ② | **HTML `<ul>`** | 최종 [0] 블록 |

- **AI에 전달되는 것**: (a) findings JSON(코드 산출), (b) `ANOMALY_KNOWLEDGE.md` 텍스트(②③ system), (c) target_lot_id.
  → **측정 raw/피벗 데이터, reformatter, 이미지는 전달하지 않습니다.** 토큰·보안 관점에서 "코드가 요약한 Finding"만 넘깁니다.
- `metrics_dict`는 인자로 받지만 현재 프롬프트 구성에는 직접 넣지 않습니다(향후 확장 여지).
- **어느 단계든 예외/`llm_fn is None`이면 즉시 `None` 반환** → [0]엔 코드 분석만.

### 4) 결과 반영

`interpret_with_ai`의 HTML은 상단에 *"※ AI가 자동 생성한 참고용 요약"* 안내와 함께 감싸져,
[0] Anomaly Summary 섹션 **맨 위**에 코드 통계 요약보다 앞서 배치됩니다.

```
[0] Anomaly Summary
├─ (AI 있으면) AI 다단계 해석 결과      ← interpret_with_ai (HTML <ul>)
├─ 통계 기반 자동 분석 (상위 5건)        ← render_findings_html (코드, AI 무관 항상)
└─ Anomaly Trend Chart + spec-out WF MAP
```

### 호출 흐름 요약

```
[프로그램 시작] .env → gpt_client 연결테스트 → _build_llm_fn() → _LLM_FN
                ANOMALY_KNOWLEDGE.md 읽기 → _ANOMALY_KNOWLEDGE_TEXT
        │
[리포트마다] analyze_commonality() → code_findings (AI 없이 항상)
        │   토글/조건 만족?
        ▼ (yes)
interpret_with_ai(findings, metrics, knowledge_text, _LLM_FN, ...)
   ①LLM(triage) → ②LLM(root-cause, +지식) → ③LLM(final, +지식) → HTML <ul>
        │ (실패/None)                              │
        ▼                                          ▼
   [0]엔 코드 분석만                       [0] 최상단에 AI 요약 삽입
```

### ANOMALY_KNOWLEDGE.md — 페르소나·답변 스타일

②③ 단계 system 프롬프트에 주입되는 **페르소나·응답 스타일** 가이드입니다(`My_config.anomaly_knowledge_path`).

- AI의 **말투/형식/태도**(간결·객관·근거 기반, HTML `<ul>` 형식, 재측정 우선 등)만 정의합니다.
- **분석 로직·불량 모드 규칙은 여기에 두지 않습니다** → [로직 확장 가능성](#로직-확장-가능성--분석-로직-카탈로그) 참조.
- **엔지니어가 이 MD만 편집하면** 코드 수정 없이 AI 해석의 톤/형식이 바뀝니다.

### LLM 연결 / 토글

- **transport 교체 가능** (`Main._build_llm_fn`): 실 환경 `gpt_client`(OpenAI 호환, `gpt-oss-120b`) / 로컬 `gpt_oss_client.mock_llm` / 둘 다 없으면 `None`.
- 3단계 오케스트레이션은 **코어(`anomaly_engine`)에 있어 그대로 이식**됩니다(사내는 `_build_llm_fn`만 실 클라이언트로 교체).
- 토글(`My_config.py`):
  - `use_gpt_summary` — AI 사용 마스터 스위치 (False면 AI 호출 자체 안 함)
  - `use_gpt_multistep` — 3단계 해석 사용 (`use_gpt_summary=True`일 때)

---

## 설정 가이드 (My_config.py)

### 불량 통계 분석 임계값

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `anomaly_lot_median_sigma` | `2.0` | `MEDIAN_SHIFT` 판정 σ 임계 (deviation 초과 시) |
| `anomaly_lot_dispersion_ratio` | `1.5` | `DISPERSION` 판정 배수 임계 (std_ratio 초과 시) |
| `anomaly_deviation_sigma` | `1.5` | Trend chart 항목 보충 선정 시 이상 판정 σ |

> `anomaly_trend_slope_sigma` · `anomaly_split_separation` · `anomaly_site_recurrence_min_lots` · `anomaly_pchk_check` 키는 과거 detector의 잔여 설정으로 **현재 `analyze_commonality` 동작에는 영향을 주지 않습니다**(불량 모드/측정 의심/drift/split 해석은 AI + 지식베이스로 이전됨).

### Anomaly Trend Chart / WF MAP

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `show_anomaly_trend_chart` | `True` | [0] Trend chart 섹션 ON/OFF (마스터) |
| `anomaly_trend_chart_top_n` | `5` | Trend chart 최대 개수(이상+주의 합산, 요약 상위와 동일) |
| `anomaly_wfmap_specout` | `True` | 이상 항목 우측 spec-out WF MAP 표시 |
| `anomaly_wfmap_max_count` | `25` | spec-out WF MAP 총 표시 상한(타깃 spec wafer는 항상 전부) |
| `scoreboard_wfmap_min_pts` | `50` | Score Board(HTML)에 wafer별 WF MAP 넣을 최소 측정 point 수 |

### Score Board 색상 (연속 보간)

- `score_color_scale` = `[(점수,'#hex'), …]` 제어점 선형보간 (PPT·HTML 공통).
- `score_color_scale_by_item` — ITEM(ALIAS)별 스케일 override (예: 특정 항목은 90점도 빨강).
- `score_color_na` — 측정 없음(N/A) 색.
- 값→색은 **`GLOBAL_CONFIG.score_color(value, item)` 단일 함수**가 결정 → HTML·PPT 색 완전 일치. 글자색은 배경 휘도로 자동.

### 기타

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `use_gpt_summary` / `use_gpt_multistep` | `True` | AI 사용 / 다단계 해석 토글 |
| `ppt_chart_dpi` / `ppt_chart_jpg_quality` / `ppt_map_jpg_quality` | 150 / 70 / 60 | PPT 차트 해상도·품질 (10MB 한도 가드) |
| `img_quality_low` / `img_quality_high` | 12 / 95 | 메일용 / 아카이브용 이미지 품질 |
| `trend_tkout_agg` | `{'MAWIN':'P10'}` | 특정 항목은 site 전체 대신 tkout별 집계점(P10/P90/MEDIAN/MEAN)으로 Trend 표시 |
| `vramp_lookback_days` | 365 | VRAMP MAX 조회 기간(일) |

---

## 리포트 구성 요소

### Score Board / WF MAP

- **HTML Score Board**: 컬럼 = `(FAB_LOT_ID, WAFER_ID)`. 같은 root의 형제 lot을 평균으로 합치지 않고 **lot별로 분리** 표시(타깃 lot 맨 왼쪽·연녹 강조 → reference → 형제 lot, lot내 wafer 오름차순).
- **wafer별 WF MAP 행**: 임계(`scoreboard_wfmap_min_pts`=50) 이상 측정된 index는 점수행 **아래에 WF MAP 행**을 추가, 각 wafer 열에 그 wafer의 WF MAP(첫 TKOUT) 삽입. sparse wafer는 빈칸.
- **PPT Score Board**(`insert_score_board`): (lot, wafer) MultiIndex 헤더(lot 가로 병합), ITEM 세로 병합, 타깃 lot 먼저. 셀 색 임계(99/95/90/그외)와 테두리.
- 색은 PPT·HTML 모두 `GLOBAL_CONFIG.score_color()`를 호출하므로 동일합니다.

### PPT 차트 / 다중 lot

`insert_plots()`가 항목별 PPT 페이지(Box / Trend / WF MAP / Radius / CDF)를 그립니다.

- **WF MAP**: wafer당 최대 **13×13 칩 격자**, 격자에 맞춰 마커 자동 축소. **같은 PGM(pt)/subitem은 한 줄**(행=PGM, 열=wafer 전체)에서 폭을 꽉 채워 최대 크기. sparse wafer는 측정 칩만 표시.
  - **색 = `REPORT DIRECTION`**(`_wfmap_cmap`): `LOWER`→낮은값 빨강/높은값 파랑(`coolwarm_r`), `UPPER`·`BOTH`→낮은값 파랑/높은값 빨강(`coolwarm`).
  - 단일 컬러바를 풀높이로 공유, 스케일 = 모집단 1~99% + lot 범위. 좌표는 zone_define(coordinate xlsx)과 inner-merge.
- **임시 차트 파일 없음**: 모든 PPT 차트는 디스크가 아니라 **메모리(BytesIO)**로 처리(루트에 `tmp_*.jpg` 미생성). HTML이 재참조하는 Trend PNG만 `RUN/TEMP/<alias>.png`로 저장.
- **다중 lot_id**(같은 root_lot_id가 같은 step에 함께 reporting): 리포트 단위 = `root_lot_id + step`(match_key)로 형제 lot이 함께 그려짐.
  - **Trend** = lot별 **색**, **Radius/Cumulative** = lot별 **marker**(색은 wafer). 모집단 median 선은 연회색(`#cccccc`).
  - 단, **spec-out(이상) 분류는 타깃 lot만**.

### ADDP 파생 항목 (Reformatize)

`Reformatize()`가 reformatter의 ADDP FORM을 계산해 파생 컬럼을 만듭니다.

- 지원: 사칙연산 / `rmax`·`rmin`(행단위 최대·최소) / `ABS`·`LOG`·`POWER`·`sqrt` / **`MA_Window`(다중 출력)**.
  - ⚠️ `STD`/`AVG`/`stddev`는 현재 미동작(Reformatize 시점엔 root_lot_id/wafer_id/tkout_time이 인덱스라 컬럼 참조 불가).
- **단일 출력**(예: `rmax({VTH_N},{VTH_P})`) → `{ALIAS}` 1개 → 페이지 1장.
- **다중 출력**(`MA_Window(...)`) → `{ALIAS}`, `{ALIAS}_minus_margin`, `{ALIAS}_plus_margin`, `{ALIAS}_ovl_index`, `{ALIAS}_new` → 각각 페이지화. (① Reformatize 생성 ② Main 컬럼 필터 `derived_addp` 보존 ③ insert_plots 페이지 목록 `{ALIAS}`+`{ALIAS}_*` 포함 — 셋 다 필요)
- ⚠️ 콤마가 든 ADDP FORM(`rmax({A}, {B})`, `MA_Window([0,1,2], …)`)은 reformatter CSV에서 **반드시 큰따옴표로** 감싸야 컬럼이 안 깨집니다.

---

## 로직 확장 가능성 · 분석 로직 카탈로그

> 이 시스템은 **코드 최소 수정으로 분석 로직을 계속 얹을 수 있게** 설계됐습니다.
> 아래는 "어디에 무엇을 추가할 수 있는가"와, 추가할 수 있는 **분석 로직 아이디어 · 참고 지식**입니다.
> (이 내용은 과거 `ANOMALY_KNOWLEDGE.md`에 있던 로직/지식으로, 문서화 목적상 README로 이관했습니다.)

### 확장 포인트 (어디에 로직을 추가하나)

| 확장 대상 | 위치 | 방법 |
|---|---|---|
| **새 이상 detector** | `anomaly_engine.analyze_commonality()` | 항목 루프 안에 `findings.append(_finding(sev, type, item, title, detail))` 한 줄 추가. 각 detector는 try/except로 독립 → 하나 실패해도 나머지 계속 |
| **판정 임계값** | `My_config.py` | `anomaly_lot_median_sigma`, `anomaly_lot_dispersion_ratio` 등 상수만 조정 (코드 불변) |
| **AI 해석 톤/형식** | `ANOMALY_KNOWLEDGE.md` | 페르소나·스타일 텍스트 편집 (로직 아님) |
| **AI 다단계 프롬프트** | `anomaly_engine.interpret_with_ai()` | triage→root-cause→final 각 단계 프롬프트 문자열 수정 |
| **파생 지표(ADDP)** | `reformatter/<vehicle>_reformatter.csv` + `Reformatize()` | CSV에 `ADDP FORM` 행 추가. 새 연산 함수는 `Reformatize.addpf` 내부에 정의 |
| **WF MAP 종류/색** | `render_*_wfmap*` + `_wfmap_cmap`/`_wfmap_norm` | 색 규칙(diverging/기준)·격자 로직 교체 |
| **리포트 섹션** | `Main.py` HTML 조립부(`target0~N`) + `My_config._REPORT_HTML_TEMPLATE` | 새 `<div id="targetN">` 자리표시자 + 채우는 코드 추가 |
| **근거 데이터 산출물** | `analyze_commonality` → `RUN/TEMP/anomaly_basis_<lot>.json/.csv` | 판단 근거(spec-out wafer/robust 산포/이탈도)를 파일로 축적 (후처리·감사용) |

**설계 원칙**: 통계 계산은 `insert_plots`(→`metrics_dict`)와 `analyze_commonality`(robust MAD/IQR)로 나뉘고, **불량 모드 조합 해석은 코드가 아니라 AI + 아래 참고 지식**에 위임합니다. 코드 detector는 "항목 단위 단일 이상"만 산출해 단순·견고하게 유지합니다.

### 넣을 수 있는 분석 로직 (아이디어)

현재 코드 detector로 구현됐거나, detector로 **추가 가능한** 분석들:

- **spec-out 판정** *(구현됨)* — 방향(`REPORT DIRECTION`) 반영, wafer별 pt 개수 그룹핑.
- **median 이탈 (robust)** *(구현됨)* — worst wafer의 median이 모집단(같은 vehicle) 대비 몇 σ(MAD 기준) 이탈.
- **산포 확대 (robust)** *(구현됨)* — lot 산포(MAD)가 모집단 대비 몇 배.
- **lot내 집단 분리(bimodality)** — 한 lot이 High/Low 두 집단으로 갈림 → 설비 챔버/슬롯 split 후보. (히스토그램 이봉성/gap 검정으로 detector화 가능)
- **동일 site spec-out 재발** — 같은 chip(X,Y)가 여러 lot에서 반복 이탈 → systematic(레티클/척/프로브핀). (위치별 재발 카운트로 detector화 가능)
- **Trend drift** — baseline(타 lot) 일별 median의 기간 기울기 → 타겟 이동/소모품 수명. (일별 회귀 기울기로 detector화 가능)
- **가속 DOE 추세** — 가속도순 TEG 묶음의 이상도 단조 증가 여부로 불량모드 working 판정.
- **PCHK 측정 신뢰성** — spec-out site가 PCHK 이탈 site와 동일하면 측정 기인 의심 → 재측정 우선. (WF MAP 제외 키워드 `wfmap_exclude_keywords`와 연계)

### 불량 모드 판정표 (참고 지식)

> **코드는 단일 이상만 산출**하고, 여러 Index 조합→불량 모드 판정은 **AI 연결 시** 아래 표를 근거로 수행합니다(위가 우선).
> 사내 이식 시 실제 Index명/대시보드 URL로 교체하세요. (예시)

1. **Contact 미오픈 불량** — 조건: `RCNT_N`/`RCNT_P` spec-out → Contact 저항 초과(식각 미오픈/폴리머 잔류).
2. **Gate 모듈 불량 (VTH)**
   - 2-1. Gate CD/VTH 연동: `VTH_N` **AND** `VTH_P` 동시 spec-out → Gate CD 미달/Oxide 산포.
   - 2-2. N-MOS 단독: `VTH_N` spec-out AND `VTH_P` 정상 → N-Well 이온주입/채널 도핑 산포.
3. **구동전류(IDSAT) 불량** — `IDSAT_N`/`IDSAT_P`/`IDSAT_RATIO`/`IDSAT_SUM` spec-out → 이동도·CD·접합/콘택 저항. VTH 동반 여부로 Gate vs 구동 구분, RATIO 이동 시 N/P 비대칭.

### 통계 패턴 → 추정 원인 카탈로그

| # | 패턴(현상) | 추정 원인 | 확인 포인트 |
|---|---|---|---|
| 1 | **lot내 집단 분리**(bimodality) | 설비 챔버/슬롯 split, 프로브카드 차이, 로트 분할 투입 | 갈린 wafer군을 설비/챔버/슬롯 이력과 대조 |
| 2 | **lot 산포 확대**(std↑) | 측정 불안정(프로브 접촉), 공정 균일도 저하 | WF MAP에서 공간적(엣지/센터)인지·특정 wafer 기인인지, PCHK 동반 여부 |
| 3 | **lot median 이탈**(spec 내) | 공정 타겟 시프트, 직전 공정 인자 변동, 캘리브레이션 | 연관 index 동일 방향 동반 여부, Trend로 drift 판단 |
| 4 | **동일 site 재발** | 레티클/척 핫스팟, 프로브카드 핀, 스캐너 필드 | 위치가 레티클 내 동일 좌표인지, 프로브카드 교체 이력 |
| 5 | **Trend drift**(baseline) | 공정 타겟 drift, 소모품 수명, 캘리브레이션 drift | 기울기 시작 시점 vs PM/소모품/레시피 변경 시점 |
| 6 | **VTH 동시 이동(N&P)** | Gate CD 미달/초과, Gate Oxide 두께, 워크펑션 금속 | Gate CD/Oxide 인라인 계측 대조 |
| 7 | **VTH 단독 이동(N 또는 P)** | 해당 극성 Well/채널 이온주입 도즈·에너지 산포 | 해당 Implant SPC, 도즈 모니터 |
| 8 | **IDSAT 이동** | 이동도(스트레스/스페이서), CD, 접합/콘택 저항 | VTH 동반 여부로 Gate vs 구동 구분, RATIO로 N/P 비대칭 |
| 9 | **PCHK 동일 site 이탈** (최우선 caveat) | 실제 불량이 아니라 **측정 오류**(프로브 접촉/콘택 저항) 가능 | 재측정으로 재현성 확인 — **불량 판정 전 먼저 검토** |
| 10 | **가속 DOE 추세** | 가속(전압/온도/시간)에 따른 열화 진행 | 이상도 단조 증가면 불량모드 working, 단발이면 warning |

---

## 주요 함수

### Main.py
| 블록 | 설명 |
|----------|------|
| 진입/트리거 파싱 | `python Main.py <vehicle>` 또는 `_TRIGGER_<vehicle>_<lot>_<step>` |
| ET 쿼리 → Hive parquet | 빅데이터 서버 조회 후 daily 파티션 저장 |
| DuckDB 로드 | viewing_period 범위 로드 + Scale Factor / ADDP 연산 + 피벗 |
| `analyze_commonality` 호출 | 통계 Finding 산출(HTML [0] + PPT 상세 공용) |
| `interpret_with_ai` 호출 | (선택) AI 다단계 해석 |
| [0] HTML 조립 | AI 블록 + 통계 요약 + Trend chart 그리드 |

### My_Function.py
| 함수 | 설명 |
|------|------|
| `etdata_query` / `inlinedata_query` / `wipdata_query` | ET / 인라인 / WIP 데이터 쿼리 |
| `insert_score_board` | PPT Score Board 테이블 |
| `insert_plots` | PPT 항목별 차트(Box/Trend/WFMap/Radius/CDF) + `metrics_dict` 산출 |
| `insert_findings_page` | PPT 'Anomaly 상세(통계)' 페이지(전체 Finding) |
| `render_wafer_wfmaps_b64` | Score Board wafer별 WF MAP |
| `render_specout_wfmaps_b64` | [0] 섹션 spec-out WF MAP |
| `Reformatize` | ADDP 수식 컬럼 계산 |

### anomaly_engine.py
| 함수 | 설명 |
|------|------|
| `analyze_commonality` | 코드 기반 Index별 단일 Finding 산출 (**AI 없이 동작**) |
| `interpret_with_ai` | Finding → AI 다단계(triage→root-cause→final) 해석 HTML (LLM 교체 가능) |
| `render_findings_html` | Finding 리스트 → HTML(상위 top_n, 신호등 색) |
| `run_anomaly_pipeline` | (구) Z-score Trend 차트 — 하위호환용 유지 |

---

## 데이터 흐름

```
빅데이터 서버 (ET test)
    │  etdata_query()
    ▼
Hive 파티션 Parquet (RUN/DB/vehicle_A_daily/date=YYYY-MM-DD/data.parquet)
    │  DuckDB read_parquet(hive_partitioning=true)
    ▼
인메모리 Raw DataFrame
    ├─▶ Scale Factor 적용 (reformatter REAL 항목)
    ├─▶ ADDP Formula 계산 (Reformatize)
    └─▶ Pivot (세로→가로, merged_df)
         │
         ├─▶ insert_plots()  ──▶ PPT 차트 + metrics_dict
         │                          │
         ├─▶ analyze_commonality(merged_df, target_lot, metrics_dict, spec)  ──▶ code_findings
         │                          │
         │                          ├─▶ render_findings_html  ─▶ [0] 통계 요약(HTML)
         │                          ├─▶ insert_findings_page  ─▶ PPT 상세 페이지
         │                          └─▶ interpret_with_ai (선택) ─▶ [0] AI 해석(HTML)
         │
         ├─▶ Score Board (Pass Rate + WF MAP)
         └─▶ HTML 조립 → 저장 + PPT 저장 + (사내) S3 업로드 / 메일 발송
```

---

> 임계 σ/배수는 합성(더미) 데이터에서는 lot간 산포가 작아 크게 나올 수 있습니다. **실제 데이터 기준으로 `My_config.py`에서 조정**하세요.
