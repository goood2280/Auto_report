# Scheduler / Trigger 계약 — OpenCode·외부 생산자

2026-09-30 `Scheduler.py` 기준. OpenCode 또는 flow의 요청 생산자는 **큐에 접수만** 하고,
같은 설치 폴더의 Scheduler 하나가 Main subprocess를 순서대로 실행한다.
외부 S3 sync/다운로더는 이 저장소 밖의 구성이다. 새 LLM 서버/관리 웹을 요구하지 않는다.

## 1. 실행 순서와 요청 상태

```text
OpenCode / flow → --enqueue 또는 inbox JSON
  → pending(영속 대기열)
  → active(실행 전 영속 claim + run_id)
  → 현재 Main 완료 후 수동 작업 1건씩 실행
  → done / failed / unknown 이력

Scheduler 제품 순회 · 수동 보고서 · Daily/ML Main
  → 같은 RUN/OPS/locks/executor.lock
  → 무거운 분석 하나씩, 업로드·워커 종료 후 잠금 해제

DB setting 전용 TRIGGER / --init-db
  → executor 잠금 우회, 제품 잠금 유지
  → 겹치지 않는 날짜 구간 병렬 조회(spawn, 공용 자원 한도)
  → 날짜 snapshot 원자적 교체 + 잠금 안에서 ET 로그 병합
```

- 이미 실행 중인 Main을 선점하지 않는다. 사이클 시작, 각 제품 시작 전, 유휴 poll 때 큐를 확인한다.
- 확인 한 번당 `trigger.max_per_check`(기본 20)건만 처리한다. 이어서 정규 순회도 진행한다.
  앞선 요청/현재 제품/일일 작업/다음 확인 시점에 따라 대기하므로 최대 대기시간을 보장하지 않는다.
- `trigger.max_pending` 기본 200건. 넘는 요청은 inbox/JSONL에 남겨 이후 수집한다.
  이는 전체 디스크 요청 용량의 제한이 아니므로 생산자는 폭주 접수를 하지 않는다.
- 상시 소비기 하나만 실행한다. `--force`도 살아 있는 Scheduler OS 잠금은 우회하지 못한다.
- `scheduler.execution_lock_wait_sec` 기본 10800초를 `AUTO_REPORT_EXECUTION_WAIT_SEC`로 Main에 전달한다.
  자식 제한시간은 작업 예산(`main_timeout_sec` 또는 서비스 `report_timeout_sec`) + 잠금 대기 예산이다.
- 공통 실행 잠금은 같은 OPS 경로를 쓰는 작업 사이에서 적용한다. 다른 설치 경로/계정은 별도 확인한다.

## 2. 접수: CLI를 우선 사용

제품/Lot/Step·분석 범위·발송 의도를 확인한 뒤 인수 배열로 JSON을 전달한다.

```python
import json, subprocess, sys, uuid
req = {
    'req_id': 'opencode-' + uuid.uuid4().hex,
    'kind': 'report', 'vehicle': 'vehicle_A',
    'lot_id': 'A488GA.1', 'step_id': 'CC942300',
    'mode': 'TRIGGER', 'generate_only': True, 'force': False,
    'requested_by': 'opencode', 'note': '파일 생성 요청',
}
subprocess.run([sys.executable, 'Scheduler.py', '--enqueue',
                json.dumps(req, ensure_ascii=False)], check=True)
subprocess.run([sys.executable, 'Scheduler.py', '--request-status',
                req['req_id']], check=True)
```

예제 값은 자리표시자다. `--enqueue`는 제품 설정·kind·형식·발송 수신처를 검증하고
inbox 파일을 원자적으로 공개한다. **Main을 실행하지 않는다.**
req_id를 생략한 CLI는 `manual-<UUID>`를 생성한다. 같은 ID의 inbox 파일을 덮어쓰지 않는다.
CLI 접수 성공 뒤에도 Scheduler의 처리 결과를 확인해야 한다.
CLI 공개는 같은 폴더의 임시 파일을 hard link로 연결하므로 inbox 파일시스템이 hard link를 지원해야 한다.
지원하지 않는 공유 파일시스템은 안전하지 않은 덮어쓰기 폴백 없이 실패한다. 아래 외부 파일 생산자 규약으로 연동한다.

### 요청 필드

| 필드 | 내용 |
|---|---|
| `req_id` | 외부 생산자는 항상 고유 ID 지정. 영숫자로 시작, `[A-Za-z0-9._-]`, 최대 128자. CLI 생략 시 자동 생성. 직접 파일에 없으면 대상 조합으로 중복 판정 |
| `kind` | `report`(기본), `init_db`, `send_user`. `trigger.allowed_kinds`에 있어야 함 |
| `vehicle` | 실제 `reformatter/config.yaml` 키. 영숫자·`.`·`_`·`-`, 1–64자. 설정을 읽지 못하면 유효 제품이라고 추측하지 않음 |
| `lot_id`, `step_id` | report/send_user 대상. 각 토큰 영숫자·`.`·`-`, 1–40자. `_` 불가. 쉼표 목록 지원 |
| `key` | vehicle/Lot/Step 대신 `제품_Lot_Step`. 오른쪽 두 `_`로 분리 |
| `mode` | `TRIGGER`(기본), `SINGLE`, `NORMAL`, `FORCE`, `ALL` |
| `mode=DB_SETTING` | `kind=init_db`로 정규화. Lot/Step 없이 제품의 원시 DB만 적재 |
| `days`, `parallel` | init_db/DB_SETTING 전용, 1 이상의 JSON 정수. 생략하면 제품 db_setting_days/db_setting_parallel(기본 200/1) |
| `generate_only` | JSON boolean. 기본 false. true면 메일·S3 OFF, 보고서 파일 생성 |
| `force` | JSON boolean. 기본 false. 대상 완료 중복만 우회. req_id/대기 중복·잠금·입력 검증은 유지 |
| `email_receiver` | 메일링 Excel 그룹명 배열(호환: 쉼표 문자열). 생략 시 trigger 기본 그룹. 실제 발송에서는 비어 있거나 허용목록 밖이면 실패, 다른 그룹으로 폴백하지 않음 |
| `send_user` | `kind=send_user` 전용, 도메인 없는 사내 ID 1명. 메일링 Excel 사용 안 함 |
| `requested_by`, `requested_at`, `note` | 운영 추적용. 비밀/원천 데이터/전체 수신자 명단을 넣지 않음 |

`force`/`generate_only`에는 문자열 `"false"`를 넣지 않는다.

| mode | 분석 범위 |
|---|---|
| TRIGGER | 제품 기본 조회기간의 비교 데이터 + 지정 대상 |
| SINGLE | 조회기간 제한 없이 지정 Lot/Step ET만 |
| NORMAL | 좌표 파일의 13pt shot만 |
| FORCE | 측정일 2일 전까지 비교기간 확장. 실제 그룹 발송은 수신 그룹 정확히 1개 |
| ALL | CAT2 전 항목 Trend. 실제 그룹 발송은 수신 그룹 정확히 1개 |

여러 Lot/Step은 같은 개수면 순서대로 짝, 한쪽 1개면 공통 적용한다.
`L1,L2` + `S1,S2` → L1-S1, L2-S2. 모든 조합 확장은 없다. 중복 제거 후 최대 100쌍이다.
원래 보고서와 같은 재발행을 원하면 비용만 보고 SINGLE로 바꾸지 않는다.
보고서 TRIGGER는 제품 YAML의 DB_Setting_mode/ptype_lot_turnoff가 True여도 이번 실행에 False로 덮어쓰고,
report_making=True를 적용한다. YAML 원본은 보존한다. generate_only=true는 여전히 메일·S3를 끈다.

- 그룹 재발송: 검증·승인한 그룹을 `email_receiver`로 지정,
  `generate_only=false`, 새 req_id + `force=true`.
- 개인 발송: `kind=send_user`, `send_user=user.id`, mode TRIGGER/SINGLE.
  generate_only와 함께 쓰지 않는다. 그룹 발송 완료 대상으로 기록하지 않는다.
- DB 초기 적재: `{"req_id":"opencode-...","kind":"init_db","vehicle":"vehicle_A","days":30,"parallel":4}`.
  `Main.py --init-db vehicle_A --days 30 --parallel 4`로 오늘 포함 30일, 병렬 조회 최대 4개를 요청하며 리포트·메일·S3는 OFF.
  Lot/Step은 필요 없고 그룹 발송 완료 대상으로 기록하지 않는다.
  `mode=DB_SETTING`도 같은 요청이다. 실제 조회 수는 공용 CPU·메모리·슬롯 한도를 따른다.
  큐의 순차 소비는 유지한다. 별도 실행을 승인한 적재는
  `Main.py "_TRIGGER_DB_SETTING_vehicle_A" --days 30 --parallel 4`로 executor를 기다리지 않고 시작할 수 있다.
  같은 제품 잠금은 유지하므로 그 제품의 다른 Main이 실행 중이면 기다린다.

### 외부 파일 생산자

기본 inbox는 `RUN/QUEUE/inbox`(`trigger.inbox_dir`).
요청 1건 = `<req_id>.json` 1개로 둔다. 같은 폴더에 `*.json.tmp`를 완성한 뒤
기존 파일을 덮어쓰지 않도록 `.json`으로 공개한다. Scheduler는 `.json`만 읽는다.
진행 중 요청의 inbox 원본은 완료까지 남아 있으므로 생산자가 수정/삭제하지 않는다.

기존 JSONL `RUN/QUEUE/trigger_queue.jsonl`도 지원한다(`trigger.queue_file`).
한 줄 = 같은 스키마의 JSON 또는 평문 key, 반드시 개행으로 끝낸다.
동시 생산자는 파일 잠금 없이 JSONL을 함께 쓰지 않는다. 새 연동은 독립 inbox 파일을 쓴다.
JSONL은 append만 하고 자르거나 다시 쓰지 않는다. offset은 소비기가 관리한다.

## 3. 중복 방지·복구와 전달 보장의 범위

1. `history`에 같은 req_id가 있으면 실행하지 않는다. 이력 기본 보관은 `history_keep=2000`건이다.
2. `dedup_by_target=true`면 큐에서 성공한 그룹 발송 대상 `vehicle|lot|step`을 차단한다.
   여러 대상이면 완료된 쌍만 제외한다. 명시적 재발행은 새 req_id와 force를 쓴다.
3. 같은 req_id/대상 요청이 pending/active에 있으면 중복 접수로 실행하지 않는다.
4. 실행 전 pending에서 빼고 `active={request,run_id,result_path,child_pid}`를 영속 저장한다.
5. 재시작 때 이전 자식 PID 또는 POSIX의 저장된 프로세스 그룹이 살아 있으면 새 소비기를 시작하지 않는다.
   matching run_id의 결과 JSON이 있으면 성공/실패를 반영하고, 결과가 없으면 unknown으로 종료 기록한다.
6. `max_retry=0`가 기본이다. 값을 올려도 **Main 프로세스 시작 실패(rc=-1)만** 재시도한다.
   timeout·중단·일반 실패·결과 누락은 자동 재실행하지 않는다.

이는 중복 실행을 줄이고 불명확한 재발송을 막는 규약이다.
외부 메일 API까지 포함한 **정확히 한 번 전달 보장**이 아니다.
비정상 종료 뒤 잔여 워커 탐지·정리는 서버 OS에서 확인해야 한다.
API가 메일을 받은 뒤 응답을 잃을 수 있으므로 unknown에서는 운영 이력·생성물·실제 수신 여부를 확인한다.
같은 요청을 즉시 새 ID로 다시 넣거나 state/history/done_targets를 고쳐 우회하지 않는다.

## 4. 결과 조회

```bash
python Scheduler.py --status
python Scheduler.py --request-status <req_id>
```

두 조회 명령은 YAML seed/RUN/log/DB를 생성하지 않는다. `--request-status`는 JSON을 출력한다.

| status | 뜻 |
|---|---|
| inbox | 파일 접수됨, 아직 영속 대기열/실행으로 수집되지 않음 |
| pending | 대기열 접수됨 |
| running | active로 claim됨. Main이 공통 실행 잠금을 기다리는 시간도 포함 |
| done | Scheduler가 Main 성공 결과를 확인함. 메일 세부 상태는 별도 확인 |
| failed | 입력/허용목록 오류 또는 확인된 실행 실패 |
| unknown | 재시작 등으로 완료 결과를 확정할 수 없음. 자동 재전송 금지 |
| not_found | 현재 inbox/상태 이력에 없음. 이력 보관 범위 밖일 수도 있어 미실행으로 단정하지 않음 |

기본 저장 위치(`trigger.queue_root`, `watchdog.ops_root` 등 설정에 따라 달라질 수 있음):

| 위치 | 읽을 내용 |
|---|---|
| `RUN/QUEUE/scheduler_state.json` | cycle·pending·active·history·done_targets·JSONL offset |
| `RUN/QUEUE/scheduler_status.json` | PID·phase·갱신시각·대기/active 스냅샷 |
| `RUN/QUEUE/done/<req_id>.json` | 처리 후 옮긴 요청 원본. 중복 스킵도 포함하므로 이 파일만으로 발송 성공 판단 금지 |
| `RUN/QUEUE/failed/<req_id>.json` | 형식 오류/실행 실패/unknown 요청 원본. 상태는 history 확인 |
| `RUN/OPS/results/<run_id>.json` | Main 실행 결과 |
| `RUN/OPS/scheduler_runs/<run_id>.json` | 종료코드·소요시간·보고서·request_id |
| `RUN/OPS/operations.sqlite` | 실행·산출물·메일 records. 존재 확인 후 mode=ro SELECT |
| `RUN/log/scheduler_log.txt`, `<vehicle>_log.txt` | 소비기/해당 제품 단계별 로그 |

요청 이력의 run_id로 운영 기록을 연결한다. 상태 스냅샷은 프로세스 생존의 보장이 아니므로
실제 PID·최근 로그도 확인한다. 접수/생성/저장/메일을 구분해서 보고한다.

## 5. 측정 history outbox와 순회 그룹

제품 실행 직후/사이클 종료에 `RUN/QUEUE/outbox/lot_history_<vehicle>.json`을 만든다.
`RUN/log/<vehicle>_et_log_Final.csv`를 Lot·DC Step 단위로 집계하며
`lot_history.out_dir`, `max_days`로 조정한다. `--export-history`는 이 출력물을 갱신하는 명령이다.

```json
{
  "schema": "auto_report.lot_history/1",
  "vehicle": "vehicle_A",
  "generated_at": "2026-09-30T11:02:00",
  "window_days": 30,
  "count": 1,
  "items": [{
    "vehicle": "vehicle_A", "lot_id": "A488GA.1", "step_id": "CC942300",
    "key": "vehicle_A_A488GA.1_CC942300", "wafer_cnt": 25,
    "tkout_time": "2026-09-30 08:41:00", "dc_done": true
  }]
}
```

`dc_done`은 정규 ET 발행 로그의 완료 표시다. 큐의 done_targets와 별도 기록이다.
명시적 재발행이면 기존 mode를 확인하고 새 req_id + force로 접수한다.
외부 flow/S3 연동은 요청 inbox와 이 outbox를 동기화하며 현재 설정 경로를 기준으로 한다.

순회 사이클 N에서 `N % every == 0`인 그룹을 등록 products 순서대로 실행한다.
위상은 state에 보존한다. `--once`는 큐와 정규 사이클 1회,
`--drain`은 설정한 처리 한도 안의 대기 요청 처리이며 독립 일일 타이머를 자동 기동하지 않는다.
둘 다 이미 실행 중인 상시 Scheduler와 함께 소비기로 실행하지 않는다.
