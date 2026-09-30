# Auto Report — 코드 수정 시 반드시 지킬 불변식

운영 요청은 먼저 `AGENTS.md`를 따릅니다. 재발행·DB 적재는 기존 CLI/큐를 우선합니다.

## 1. HTML 인라인 이미지 (절대 방식 변경 금지)
- 리포트 HTML의 **모든 `<img>`는 반드시 `data:image/...;base64,` 인라인(data URI)** 이어야 한다.
  - 파일 경로 참조(`src="RUN/TEMP/..."`), CID 첨부, 외부 URL 방식으로 바꾸지 말 것.
  - 이유: 사내 메일 본문·포워딩·단독 HTML 파일 모두에서 이미지가 동일하게 보여야 함.
- data URI 생성은 반드시 Main.py의 `_img_datauri()`를 거친다.
  - 이미지 1개당 `html_inline_img_max_kb`(My_config) 이하를 보장 — 초과 시 자동 PNG 축소 → JPEG 재인코딩.
  - 이유: 사내 메일 서버가 큰 인라인 이미지를 '첨부'로 분리해 제품마다 표시가 들쭉날쭉해지는 것 방지.
- Main.py의 "인라인 이미지 불변식 검증" 블록(HTML 저장 직전, `_bad_srcs` 검사)을 **지우거나 우회하지 말 것**.
  이미지 삽입 코드를 수정했다면 실행 로그에서 `[INFO] HTML 인라인 이미지 검증 OK`를 확인할 것.

## 2. Anomaly Trend Chart / WF MAP 표기 규칙
- SPEC OUT WF MAP의 **target 판정 = 리포트의 lot_id + step_id 조합**(둘 다 일치해야 target).
  같은 lot의 다른 step WF MAP은 target이 아니다 — root_lot이나 lot_id만으로 판정하지 말 것.
- target WF MAP 라벨 = **파란색(0,51,204), bold 아님(일반 폰트)**, 그 외 = 회색(85,85,85).
  bold는 같은 폭 셀에서 라벨을 넓혀 가독성을 해치므로 되돌리지 말 것.
- target WF MAP들은 합성 이미지 왼쪽에 **파란 테두리 박스로 묶어** 표시(나머지 그리드는 그 오른쪽,
  **테두리 없음** — 회색 테두리도 그리지 않는다).
- target의 spec-out wafer는 `anomaly_wfmap_max_count`와 무관하게 전량 표시, 남는 칸은 tkout_time 최신순.
- 판정 로직 안내문은 두 곳에 있고 **둘 다 My_config 값을 동적으로 읽는다** — 임계값을 하드코딩하지 말 것:
  - HTML: Main.py `_chart_logic` (Anomaly Trend Chart 위 안내 박스)
  - PPT: My_Function.py `_note_lines` (Anomaly 상세 1페이지 참고사항)
  - 주의(WARNING) = ① 플라이어(`anomaly_flier_sigma`/`anomaly_flier_max_pts`) ② 산포 확대
    (`anomaly_lot_dispersion_ratio` + 절대량 게이트 `anomaly_disp_min_spec_frac`). 판정 로직을
    바꾸면 이 두 안내문도 반드시 함께 갱신할 것.

## 3. trend_tkout_agg(집계) 항목 판정 규칙
- `My_config.trend_tkout_agg`에 등록된 항목(예: MAWIN=P10)은 **spec-out/이상 판정도 집계값 기준**이다.
  - anomaly_engine: `_agg_item_set` 치환 로직 (raw pt 폴백 금지 — `it not in _agg_item_set` 가드 유지)
  - My_Function `_render_item_charts` metrics: `_agg_spec`이면 집계된 `tdf`에서 spec-out 계산
  - raw shot 단위 pt로 spec-out을 세도록 되돌리지 말 것.

## 4. 로그 청결
- matplotlib 소음 억제 장치를 지우지 말 것 (로그에는 필요한 내용만 나와야 함):
  - `warnings.filterwarnings("ignore", message=".*Glyph.*")` — U+2212 글리프 경고 (Main/My_Function/anomaly_engine 3곳)
  - `logging.getLogger('matplotlib.font_manager').setLevel(ERROR)` — findfont 폰트 미발견 로그 (같은 3곳)
  - `plt.rcParams['axes.unicode_minus'] = False` — 각 차트 함수 진입부
  - My_Function의 필터는 병렬 워커가 import하는 경로라 특히 중요(지우면 워커에서 재발).

## 5. Scheduler (제품 순회 + 수동 트리거)
- `Scheduler.py`는 Main.py를 **반드시 별도 프로세스(subprocess)로** 실행한다.
  import 해서 `main()`을 직접 부르면 병렬 차트 워커가 `__main__`을 재import하며 발행이 중복된다.
- 제품 순회·보고서 TRIGGER·Daily Trend·ML은 Main의 `_execute_serially`에서
  `RUN/OPS/locks/executor.lock`을 공유한다. 업로드·렌더 워커 종료가 끝나기 전에 잠금을 풀지 않는다.
  운영 LLM은 직접 Main 대신 `Scheduler.py --enqueue`로 현재 작업 뒤에 요청을 접수한다.
- DB setting 전용 TRIGGER와 `--init-db`는 executor 잠금을 우회한다. 같은 제품 잠금은 유지하며,
  일수/병렬 조회 수는 CLI 또는 제품 기본값을 사용한다. spawn 조회 프로세스 종료 후 공용 슬롯을 반납한다.
  날짜 구간은 겹치지 않게 분할하고, 완전히 같은 원시 행을 제거한 날짜 파일을 원자적으로 교체한다(append 금지).
  ET 로그 병합은 별도 OS 잠금 안에서 prime_key별 1행을 유지하며, 조회·저장 실패는 실패 종료로 전달한다.
  Categorical 날짜는 저장/집계 전에 datetime으로 정규화하며, 범주 순서를 부여해 max를 우회하지 않는다.
- 보고서 TRIGGER는 이번 실행의 DB_Setting_mode/ptype_lot_turnoff=False, report_making=True를 강제한다.
  YAML 파일은 수정하지 않으며 DB setting 적재 명령과 메일·S3 생성 전용 제약은 유지한다.
- 큐 요청은 pending에서 꺼낼 때 active claim을 먼저 저장한다. 재시작 시 결과가 불명확하면
  `unknown`으로 남기고 자동 재발송하지 않는다. 상태 저장 실패를 무시하고 Main을 실행하지 않는다.
- 순회 주기·그룹은 `reformatter/scheduler.yaml`(없으면 첫 실행 시 기본값으로 자동 생성)에서만 바꾼다.
  그룹 `every`(A=1, B=3, C=6)는 사이클 번호 % every로 판정하고, 사이클 번호는
  `RUN/QUEUE/scheduler_state.json`에 저장 — 재시작해도 B/C 위상이 초기화되지 않아야 한다.
- 트리거(강제발행) 중복 방지 3중 장치를 약화시키지 말 것(외부 메일의 정확히 한 번 전달 보장은 아님):
  ① `history[req_id]` ② `done_targets[vehicle|lot|step]`(`force:true`로만 우회) ③ 대기열 중복 검사.
  `process_triggers`에서 요청은 **처리 전에 반드시 pending에서 pop**한다(안 빼면 같은 건을 계속 재처리).
- 트리거 수신처는 환경변수 `AUTO_REPORT_EMAIL_RECEIVER`(콤마 구분) → `My_config.load_from_yaml`이
  config.yaml의 `email_receiver`를 덮어쓰는 경로 하나뿐이다. 요청 JSON이 지정한 그룹은
  `trigger.allowed_email_receiver` 허용목록으로 반드시 걸러서 쓴다(외부 입력).
- lot_id/step_id에 `_`를 허용하지 말 것 — Main.py가 `_TRIGGER_{vehicle}_{lot}_{step}`을
  `rsplit('_', 2)`로 되돌리므로 파싱이 어긋난다.
- 큐 파일 규약은 `docs/SCHEDULER_TRIGGER_CONTRACT.md`.

## 6. 배포 방식
- 기본 설치는 Main.py·Scheduler.py·My_config.py 3개와 보조 소스 ZIP을 풉니다.
- 보조 모듈 수정 시 `python setup.py --extract-sources`; 기존 파일은 보존합니다.
- 수정 후 `python gen_setup.py` 또는 `python setup.py --build`로 setup.py를 재생성합니다.
- ZIP import의 `__file__`은 가상 경로입니다. 운영 DB 경로는 설치 폴더 기준으로 유지하고,
  캐시 fingerprint는 ZIP 내용 변경도 반영해야 합니다. spawn 워커 import도 검증합니다.
- setup.py는 생성물로 직접 수정하지 않습니다. 새 파일은 gen_setup.py 목록에 명시합니다.
- Gemma4 연결과 VS Code 웹 관리 화면은 2026-09-29 사용자 요청으로 제거되었습니다.
  웹 전용 자연어 도우미·기준값 조정도 복원하지 않습니다. OpenCode는 기존 CLI를 사용합니다.

## 9. 메일 본문 그림 수 (Attach file count is over 10)
- 사내 메일 API 는 본문 data:image 도 첨부로 떼어 셀 수 있다 — **그림 + 첨부 ≤ `mail_attach_limit`(10)**.
  발행물은 만들 때 메일 1통 그림을 `_mail_image_limit()`(기본 `mail_inline_image_limit`=8) 이하로 맞춘다:
  Daily Trend = 한 줄 띠 1장(`_daily_strip_uri`), ML = 항목당 시트 1장 + 인자 차트 1장(`_ml_report_pack`), ALL = 2열 시트(`_all_trend_sheets`).
  차트마다 `<img>` 를 하나씩 넣는 방식으로 되돌리지 말 것.
- `_durable_mail` 의 `_mail_attachment_guard` 는 마지막 안전장치 — 지우거나 우회하지 말 것.

## 10. ML mode (실험 기능)
- 항목 페이지 = auto report 항목 페이지 기하(`_ML_LX/_ML_LW/_ML_RX/_ML_RW`) — 왼쪽 Box·WF MAP, 오른쪽 Trend·Radius·Cumulative.
- 인자 스크리닝은 `My_Function.ml_factor_screen` 한 곳(범주=KNOB/MASK/EQP, 수치=INLINE/VM, R²·밑둥 들림, design effect + BH).
  '밑둥 들림' = 한쪽 꼬리만 움직이고 반대쪽 꼬리는 그대로 — 양쪽이 같이 움직이는 전체 이동은 R² 가 맡는다.
- 차트 안 글자는 ASCII(`_mlv_ascii`) — 사내 서버에 한글 글꼴이 없으면 두부 글자가 된다. 한국어는 HTML·PPT 텍스트에만.

## 8. 성능·메모리·용량 (2026-09-29)
- 렌더 워커 수는 `resource_governor.plan_workers` 가 정한다: 서버 공용 슬롯(OS 파일 잠금, `AUTO_REPORT_SLOT_DIR`)으로
  여러 Main 프로세스 합계 ≤ 코어 − `parallel_reserve_cores`, 그리고 실측 CPU·메모리(cgroup 포함) 여유만큼만. 직접
  `os.cpu_count()` 로 워커 수를 정하지 말 것(여러 bash 동시 실행 시 과부하). 워커 풀은 `spawn`(fork 는 DuckDB/S3 스레드와 교착).
- `parallel_workers` 수동 지정도 요청 상한이다. 메모리·CPU·실제 확보한 슬롯 한도를 우회하지 않는다.
  조정기 실패는 직렬 처리, 풀 종료는 `shutdown(wait=True)` 후 슬롯 반납이다.
- DuckDB 는 `resource_governor.duckdb_settings` 로 threads·memory_limit 을 건다(기본값=모든 코어·RAM 80%).
- 여러 Lot 트리거 규칙은 `My_Function.trigger_pairs` 와 `Scheduler._pairs` 두 곳이 같아야 한다.
- S3 업로드는 백그라운드 스레드(`_upload_async`), 실행 이력 반영은 메인 스레드 `_drain_uploads` 만 한다.
- PPT 이미지: 차트는 `_savefig_chart`(팔레트 PNG/JPEG 중 작은 쪽), WF MAP 은 `_encode_map_canvas`(원본 해상도 팔레트 PNG —
  점 지도는 축소하면 번져서 PNG 가 커진다). 저장 직전 `fit_ppt_budget` 이 메일 한도(ppt_mail_max_mb × ppt_budget_ratio) 안으로
  Description 이미지 화질을 정하고(남은 용량 기준), 그래도 넘으면 큰 차트부터 줄인다. Description 이미지는 반드시 이 PPT 소유의
  새 파트로 넣는다(원본 설명 PPT 파트를 공유하면 용량 조절이 원본까지 바꾼다).
