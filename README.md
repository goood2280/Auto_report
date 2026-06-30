# HOL Auto Report System

> 반도체 DC 측정 결과 자동 리포팅 시스템

## 📋 목차
- [개요](#개요)
- [아키텍처](#아키텍처)
- [디렉토리 구조](#디렉토리-구조)
- [설정 파일 가이드](#설정-파일-가이드)
- [실행 방법](#실행-방법)
- [주요 함수 설명](#주요-함수-설명)
- [데이터 흐름](#데이터-흐름)

## 개요
Python 기반 반도체 HOL(Head Of Line) DC 측정 데이터 자동 분석 및 리포트 생성 시스템.
- ET(Electrical Test) 데이터 쿼리 및 Hive 파티셔닝 저장
- DuckDB 기반 고속 인메모리 데이터 처리
- Pass Rate Score Board / Inline SPC 분석
- Z-score 기반 이상 탐지 + GPT 분석 코멘트
- PPT/HTML 자동 레포트 생성 및 메일 발송

## 아키텍처

### 설정 3원화 구조
| 파일 | 역할 | 편집 대상 |
|------|------|----------|
| `My_config.py` | 전체 공통 설정 (경로, 컬럼, 임계값, 색상 등) | 시스템 관리자 |
| `reformatter/config.yaml` | Vehicle별 개별 설정 (process_id, dc_step 등) | Vehicle 담당자 |
| `GPT_MANUAL.md` | AI(GPT) 분석 프롬프트 지침 | AI 분석 설계자 |

### 파일 구조
```
auto report/
├── Main.py                  # 메인 파이프라인 (진입점)
├── My_Function.py           # 유틸리티 + PPT 생성 + 데이터 쿼리
├── My_config.py             # 글로벌 설정 관리
├── anomaly_engine.py        # Z-score 이상 탐지 엔진
├── gpt_oss_client.py        # GPT 요약 생성 클라이언트
├── bigdataquery.py          # DB 쿼리 래퍼
├── GPT_MANUAL.md            # AI 분석 지침
├── reformatter/
│   ├── config.yaml          # Vehicle별 설정
│   └── vehicle_A_reformatter.csv  # 항목 정의 (REAL/ADDP)
├── templates/
│   └── report.html          # HTML 레포트 템플릿
├── RUN/
│   ├── DB/
│   │   └── vehicle_A_daily/    # Hive 파티셔닝 ET 데이터
│   │       ├── date=2026-06-29/
│   │       │   └── data.parquet
│   │       └── date=2026-06-30/
│   │           └── data.parquet
│   ├── Report/
│   │   └── vehicle_A/
│   │       ├── HTML/           # HTML 레포트
│   │       ├── Mail/           # 메일용 PPT (저화질)
│   │       └── EDM/            # 아카이브 PPT (고화질)
│   └── log/                    # 실행 로그
└── scripts/
    └── setup_local_test.py     # 로컬 테스트 데이터 생성
```

## 설정 파일 가이드

### My_config.py (전체 공통)
- `score_thresholds`: Score Board 색상 구분 임계값 [100, 90, 70, 50]
- `score_colors`: 각 임계값에 대한 배경색/글자색 매핑
- `anomaly_z_threshold`: 이상 탐지 Z-score 임계값 (기본 3.0)
- `vramp_lookback_days`: VRAMP MAX 값 조회 기간 (기본 365일)
- `img_quality_low/high`: PPT 이미지 품질 (메일용/아카이브용)

### config.yaml (Vehicle별)
- `dc_step_to_ids`: DC Step → Step ID 매핑 (예: MFDC: ['test'])
- `viewing_period`: Score Board/차트에 표시할 데이터 기간 (일)
- `QueryTimeSpan`: ET 데이터 쿼리 범위 (일)
- `process_id`, `line_id`: DB 쿼리 필터 조건

### GPT_MANUAL.md (AI 지침)
- Z-score 해석 기준, 불량 모델 정의, 코멘트 구조 등

## 실행 방법

```bash
# 기본 실행 (vehicle_A)
python Main.py vehicle_A

# 강제 발행 (특정 LOT)
python Main.py _TRIGGER_vehicle_A_T1234.1_test

# 새 vehicle 추가 시:
# 1. reformatter/config.yaml에 새 vehicle 블록 추가
# 2. reformatter/새vehicle_reformatter.csv 생성
# 3. python Main.py 새vehicle
```

## 주요 함수 설명

### Main.py
| 함수/블록 | 설명 |
|----------|------|
| `etdata_query()` | 빅데이터 서버에서 ET 데이터 쿼리 → Hive 파티션 parquet 저장 |
| DuckDB 데이터 로드 | daily 파티션에서 viewing_period 범위 데이터 로드 + Scale Factor/ADDP 연산 |
| Score Board 생성 | Pass Rate 계산 + HTML/PPT 테이블 렌더링 |
| Inline Table 생성 | SPC 인라인 측정 데이터 UCL/CL/LCL 포함 테이블 |
| Anomaly Detection | Z-score 기반 이상 항목 탐지 + Trend 차트 3×2 그리드 |

### My_Function.py
| 함수 | 설명 |
|------|------|
| `etdata_query()` | ET 데이터 쿼리 및 Hive 파티셔닝 저장 |
| `inlinedata_query()` | 인라인 측정 데이터 쿼리 |
| `wipdata_query()` | WIP(재공) 현황 쿼리 |
| `make_title_page()` | PPT 타이틀 슬라이드 생성 |
| `insert_score_board()` | PPT Score Board 테이블 삽입 |
| `insert_plots()` | PPT 차트 슬라이드 생성 (Box/Trend/WFMap/Radius/CDF) |
| `Reformatize()` | ADDP 수식 컬럼 계산 |

### anomaly_engine.py
| 함수 | 설명 |
|------|------|
| `run_anomaly_pipeline()` | Z-score 기반 이상 탐지 + Trend 차트 HTML 생성 |

## 데이터 흐름

```
빅데이터 서버 (eds.f_et_test)
    │
    ▼ etdata_query()
Hive 파티션 Parquet (vehicle_A_daily/date=YYYY-MM-DD/data.parquet)
    │
    ▼ DuckDB read_parquet(hive_partitioning=true)
인메모리 Raw DataFrame
    │
    ├─▶ Scale Factor 적용 (reformatter.csv REAL 항목)
    ├─▶ ADDP Formula 계산 (reformatter.csv ADDP 항목)
    └─▶ Pivot (세로→가로 전개)
         │
         ├─▶ Score Board (Pass Rate 계산)
         ├─▶ PPT 차트 (Box/Trend/WFMap/Radius/CDF)
         ├─▶ Anomaly Detection (Z-score)
         └─▶ HTML 레포트 조립
              │
              ▼
         HTML 파일 저장 + PPT 저장 + 메일 발송
```
