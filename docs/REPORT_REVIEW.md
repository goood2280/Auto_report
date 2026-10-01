# OpenCode 수정 → 테스트 → 샘플 → 운영 반영

이 문서의 제품·Lot·ID·경로는 예시다. 실제 제품 키와 측정 이력으로 대조한다.
설치 폴더를 OpenCode에서 열고 요청해도 된다. 명시적인 개선 요청은 아래 후보 폴더에서 수행한다.
운영 조회·재발행 요청은 기존 Scheduler 큐를 사용한다.

## 1. 후보 만들기

운영 중인 파일을 먼저 고치지 않는다. 운영본과 나란한 새 폴더를 만들고 소스·문서·테스트를 복사한다.

```bat
python report_review.py prepare --source "D:\AutoReport" --target "D:\AutoReport-review-001"
```

후보 폴더를 OpenCode 작업 폴더로 연다. 기본 복사에는 .env, 제품 설정, DB, 큐, 메일 이력이 없다.
후보의 `.report-review.json`에는 원본 경로·파일 지문이, `.review`에는 검증·샘플 기록이 저장된다.
로컬 파일이며 원격에 올리지 않는다.
설치본에서도 필요한 보조 소스와 테스트를 번들에서 꺼내므로 개발 PC의 전체 저장소가 필요하지 않다.

실측 데이터 미리보기에는 필요한 입력을 **명시해서** prepare의 `--input`으로 복사한다.
예를 들어 제품 YAML/CSV, 최근 날짜 파티션, 해당 제품 ML_TABLE과 ET 로그, WIP 파일,
좌표 Excel·Inline 정의·설명 PPT가 필요하다. DB 전체를 기본 복사하지 않는다.

```bat
python report_review.py prepare --source "D:\AutoReport" --target "D:\AutoReport-review-002" --input reformatter --input RUN/DB/vehicle_A_daily/date=2026-10-01 --input RUN/DB/ML_TABLE_vehicle_A.parquet --input RUN/DB/vehicle_A_wip_current.csv --input RUN/log/vehicle_A_et_log.csv --input SF3_Data_Extractor_Input_File_v0.xlsx --input INLINE_1_reformatter.xlsx --input HOL_Auto_Report_Description.pptx
```

YAML의 절대 경로가 운영본을 가리키면 후보 안의 복사 경로로 바꾼다. 심볼릭 링크/정션으로 운영 DB를 연결하지 않는다.
개인 발송용 메일 자격은 현장 환경으로 별도 준비하며 `.env`를 자동 복사하지 않는다.
필요한 비교 기간의 날짜 파티션을 선택한다. 일부 데이터만 복사했다면 그 제한을 검토 결과에 적는다.

## 2. 제품별 Daily Trend/ML 항목을 자연어로 변경

OpenCode 요청 예:

- “vehicle_A Daily Trend에 VTH_N, IDSAT_N을 추가하고 LEAKAGE를 빼줘. 샘플까지 만들어줘.”
- “vehicle_A의 Daily와 ML 모두 MAWIN을 보도록 해줘. 분류는 FAB_ETCH로 해줘.”
- “vehicle_A VTH_N의 시간축을 TKOUT_TIME_ETCH로, 분류를 Recipe와 KNOB_IMPLANT 조합으로 바꿔줘.”
- “ML 항목 선택은 그대로 두고 Daily에서만 VTH_N을 빼줘.”

OpenCode는 요청을 해석하고 기존 제품 키·ALIAS를 확인한 뒤 `report_review.py items-*`로 dict를 갱신한다.
항목명에 오타나 중의성이 있으면 실제 후보를 제시하고 필요한 값만 확인한다. 새 측정 항목이나 계산식이
필요한 요청은 별도의 Auto Report 항목 정의 변경으로 다룬다.

후보 폴더에서:

```bat
python report_review.py items-list --vehicle vehicle_A --service daily_trend
python report_review.py items-add --vehicle vehicle_A --service daily_trend --item VTH_N --item IDSAT_N
python report_review.py items-remove --vehicle vehicle_A --service daily_trend --item LEAKAGE
python report_review.py items-add --vehicle vehicle_A --service both --item MAWIN --split-column FAB_ETCH
python report_review.py items-add --vehicle vehicle_A --service daily_trend --item VTH_N --time-column TKOUT_TIME_ETCH --split-column Recipe --split-column KNOB_IMPLANT
```

저장 정본은 `reformatter/report_items.yaml`이다. 다른 제품·서비스의 설정은 보존한다.

```yaml
version: 1
daily_trend:
  vehicle_A:
    VTH_N:
      time_column: TKOUT_TIME_ETCH
      split_columns: [Recipe, KNOB_IMPLANT]
    IDSAT_N: {}
mlmode:
  vehicle_A:
    MAWIN: {}
```

- 사용 가능한 측정 항목은 기존 reformatter의 **숫자 REPORT ORDER가 있는 ALIAS**다.
  ALIAS로 선택하면 해당 항목의 파생 출력과 Step/프로그램/온도별 차트도 함께 선택한다.
- 파일·서비스·제품 설정이 없으면 위 기존 Auto Report 항목 전체를 사용한다. 제품 dict가 `{}`이면
  해당 서비스에서 그 제품의 항목은 0개다. 첫 추가/삭제는 기본 전체 목록에서 요청한 변경만 적용한다.
- CAT2·SPEC·배율·단위·REPORT ORDER는 기존 Auto Report 열을 그대로 쓴다. CAT2가 비면 `Uncategorized`다.
  Daily/ML 전용 `tkout_time`, `split_check` 같은 reformatter 열은 읽지 않는다.
- 기존 별도 열을 쓰던 제품은 필요한 값을 위 dict의 `time_column`/`split_columns`로 옮긴다.
  시간축 미지정은 DC 측정 시각, 분류 미지정은 ALL이다. 실제 ML_TABLE 열 이름 또는 기존 step/pattern
  resolver를 사용하며, 없는 ML_TABLE 열은 보고서 경고로 표시한다.
- 이 dict는 Daily/ML 후보 측정 항목을 정한다. Daily의 최근 24시간 조건과 ML 통계 선별은 계속 적용된다.
  추가한 항목이 매번 이상 보고서에 나타난다는 뜻은 아니다. 일정·제품·수신처는 기존 scheduler 설정을 따른다.
- 서비스 실행마다 파일을 읽으므로 이후 정규 발행에도 유지된다. 선택 변경은 산출물·관측 체크포인트 지문에 반영된다.
  설치 업데이트는 이 로컬 YAML을 덮어쓰지 않는다. 공개 Git·번들에 실제 제품 목록을 넣지 않는다.

## 3. 검증하고 미리보기 만들기

관련 테스트를 **명시적으로** 실행한다. check 성공 기록은 그때의 코드·설정 지문에 묶인다.
테스트 실행 중 코드나 입력이 바뀌어도 성공 기록을 남기지 않으므로 변경을 마친 후 다시 check한다.

```bat
python report_review.py check --test tests/test_report_items.py --test tests/test_daily_services.py --test tests/test_ml_insight.py
python report_review.py preview --vehicle vehicle_A --service daily_trend
python report_review.py preview --vehicle vehicle_A --service mlmode
python report_review.py preview --vehicle vehicle_A --service auto --lot L001.1 --step S1
```

미리보기는 후보의 복사 DB만 사용하고 메일·S3·원천 갱신을 끈다. Auto Report의 사내 Inline 조회는 생략한다.
Inline 연동 변경이라면 모의 테스트 외에 운영 반영 전 별도 현장 검증이 필요하다.
운영 경로를 가리키는 설정은 실패시킨다. 입력이 없거나 대상이 없으면 성공 샘플로 간주하지 않는다.
후보의 생성 HTML/PPT를 직접 열어 대상·차트·통계 근거·메일 분할 수를 확인한다.

```bat
python report_review.py record --vehicle vehicle_A --html "RUN/OPS/daily_trend/<발행ID>/part-1.html" --ppt "RUN/OPS/daily_trend/<발행ID>/part-1.pptx" --title "Daily Trend 변경 검토"
```

실제 생성된 경로를 사용한다. 여러 부분으로 나뉜 보고서는 각 HTML/PPT 쌍을 등록·검토한다.
record는 파일 지문과 검증 기록을 묶은 review ID를 출력한다. 코드·선택·등록 파일을 바꾸면 다시 검증·등록한다.

## 4. 특정 사람에게 테스트 샘플 보내기

사용자가 지정한 사내 ID와 발송 의도를 확인한 후에만 실행한다. 수정 요청만으로 실제 발송하지 않는다.
이미 같은 대화에서 대상과 발송을 지정했다면 다시 묻지 않는다.

```bat
python report_review.py send-sample --review <review-ID> --user person.one --user person.two
```

제목에 TEST를 붙이고 **등록한 파일**만 보낸다. 운영 메일링 Excel의 그룹, 정규 발행 상태, S3를 사용하지 않는다.
후보 폴더의 메일 이력에서 사람별 sent/failed/unknown을 확인한다. 같은 review/사람 조합은 영속 중복 방지를 사용한다.
unknown이면 수신 여부를 확인하며 새 ID를 만들어 자동 재발송하지 않는다.
샘플 수신과 검토 승인 여부는 별개다. 수신자의 확인 내용을 사용자가 전달한 뒤 운영 반영 여부를 결정한다.

## 5. 승인 후 운영 반영

```bat
python report_review.py promotion-plan --review <review-ID>
```

이 명령은 번들을 재빌드하고, 준비 이후 운영 원본이 바뀌었는지 확인한 뒤 변경 파일·번들 지문·원본 경로를 출력한다.
설치·재시작은 수행하지 않는다. 출력과 샘플 파일·검증 결과를 보여 준 다음 사용자의 운영 반영 승인을 받는다.
원본이 바뀌었으면 새 후보로 차이를 합치고 다시 검증한다.

승인한 동일 번들과 변경 목록으로 다음 절차를 실행한다:

1. 정규 Scheduler와 독립 Watchdog/Daily/ML 타이머를 정상 종료한다. 현재 Main/렌더/업로드 완료와 PID를 확인한다.
   실행 중인 작업을 강제 종료하거나 새 Scheduler를 중복 시작하지 않는다.
2. `promotion-plan`을 다시 실행해 원본 변화와 번들 지문을 확인한다. 승인 후 바뀐 파일은 재승인 대상이다.
3. 후보의 `setup.py`를 사용해 `python "D:\AutoReport-review-001\setup.py" --target "D:\AutoReport" --preserve-config`를 실행한다.
   기존 소스는 `.setup-backups`에 보존된다. 설치 도구는 제품 YAML과 RUN을 보존한다.
4. 선택 YAML 변경은 installer가 반영하지 않는다. 운영의 `reformatter/report_items.yaml`을 로컬 백업하고
   승인한 후보 YAML을 같은 경로로 복사한다. scheduler/config YAML 변경도 diff를 검토한 파일만 명시적으로 반영한다.
   후보의 DB·OPS·QUEUE·메일 이력을 운영 폴더로 복사하지 않는다.
5. 실제 실행 소스(느슨한 .py/ZIP), 로컬 설정·기존 DB 보존을 확인한 후 기존 방법으로 Scheduler를 하나만 시작한다.
   첫 정규 작업과 Daily/ML의 산출물·메일 상태를 확인한다. 현장 미검증 부분은 별도로 보고한다.

문제가 있으면 아래의 코드 버전 복원을 사용한다. 현재 설정·운영 이력은 되감지 않는다.
후보와 검토 기록은 사용자 지시 없이 삭제하거나 원격 업로드하지 않는다.

## 6. 로컬 코드 버전과 이전 버전 복원

Scheduler 시작 로그와 각 Main 실행에 `[VERSION]`/현재 코드 버전이 표시된다.
예: `code-0123456789abcdef`와 저장 시각/라벨. 실행 결과 JSON·보고서 이력과 Daily/ML manifest에도
`code_version`이 남는다. 코드 내용이 같으면 같은 버전이고 `My_config.py`나 YAML만 바꿔도 코드 버전은 바뀌지 않는다.

운영 폴더에서:

```bat
python report_review.py current-version
python report_review.py versions
python report_review.py save-version --label "현재 정상 동작 코드"
python report_review.py rollback --version code-0123456789abcdef
```

OpenCode에 “현재 버전 알려줘”, “저장된 버전 목록 보여줘”, “code-…로 돌아가줘. 설정은 그대로”라고 요청하면 된다.
복원 요청은 목록에 저장된 정확한 ID/라벨과 대조한다. 여러 후보가 있으면 필요한 버전만 확인한다.
이미 버전을 명시해 복원을 요청했다면 다시 허락받지 않는다. 현재 작업과 독립 타이머를 정상 종료한 후 실행한다.
커스텀 scheduler YAML을 사용하는 설치본은 rollback에 `--config <현재 YAML 경로>`를 전달한다.

- 이력은 설치 폴더의 `.runtime-versions/<코드ID>/`에 저장한다. 원격 GitHub나 외부 서비스는 사용하지 않는다.
  `current.json`은 현재 기록, `events.jsonl`은 저장·복원 이력이다. 운영 상태/메일 이력과 별개이며 임의 정리하지 않는다.
- 최초 실행과 설치 전후에 실제 실행 소스를 저장한다. 느슨한 `.py`가 ZIP보다 우선하는 구조도 반영한다.
  기록된 코드만 복원할 수 있다. 이미 없어진 과거 코드가 자동 복구되지는 않는다.
- 복원은 `Main.py`, `Scheduler.py`, `My_Function.py`, `anomaly_engine.py`, `operator_console.py`,
  `resource_governor.py`, `report_items.py`의 저장된 코드와 대응 ZIP 내용을 바꾼다.
  복원 도구 `report_review.py`/`runtime_versions.py`는 유지한다.
- **`My_config.py`, 모든 YAML·reformatter CSV·.env, DB·큐·메일 이력은 현재 상태 그대로 이어 간다.**
  선택 dict도 현재 것을 쓴다. 예전 코드에 현재 설정이 호환되는지 관련 테스트·미리보기로 확인한다.
- 새 Scheduler/Main은 실행 수명 동안 공유 lease를 잡는다. 복원/설치는 배타 lease를 요구해 실행 중에는 실패한다.
  rollback은 기존 Scheduler/Watchdog/Daily/ML과 Main executor/제품 잠금도 확인한다. 잠금을 지우거나 `--force`로 우회하지 않는다.
- 복원 파일의 지문·Python 구문을 쓰기 전에 검사한다. 실패 시 변경 전 코드로 복구를 시도하며 오류와 복구 상태를 보고한다.
  복원한 코드의 Python 바이트코드 캐시도 무효화해 재시작 시 저장된 소스를 읽도록 한다. 설정 파일 캐시는 보존한다.
  완료 후 `current-version`과 재시작 로그를 확인한다. 복원 전 코드도 저장되므로 다시 그 ID로 돌아갈 수 있다.
- 도입 전 코드를 설치 전 snapshot으로 저장했다면 그 코드에는 시작 배너가 없을 수 있다.
  이때도 `current-version`으로 실제 코드를 확인한다. 설치/복원 도구와 로컬 버전 기록은 유지된다.

`--preserve-config`는 코드 업데이트 때도 현재 My_config.py를 보존한다. 후보에서 설정 파일 자체를 고쳤다면
승인한 config/YAML diff만 따로 백업·반영한다. 이후 코드 버전을 복원해도 그 설정 변경은 유지된다.
테스트에서는 `AUTO_REPORT_VERSION_STORE`를 임시 폴더로 분리한다. 운영 기본값은 설치 폴더이며
후보 기록이나 테스트 기록을 운영의 버전 이력에 합치지 않는다.

## OpenCode 완료 보고

변경 요약, 후보 위치, 관련 테스트 결과, 샘플 HTML/PPT와 review ID, 수신자별 발송 상태,
승인 대상 번들 지문과 설정 diff, 현장 검증이 필요한 부분을 짧게 보고한다.
오프라인 합성 테스트·샘플만으로 운영 발송 성공을 주장하지 않는다.
