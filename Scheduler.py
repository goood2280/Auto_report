#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==================================================================================================================================
# Scheduler.py — Auto Report 제품 순회 스케줄러 + 수동 트리거(강제발행) 큐 소비기
# ----------------------------------------------------------------------------------------------------------------------------------
# 역할
#   1) 그룹(A/B/C…)에 등록된 제품(vehicle)을 순회하며 `python Main.py <vehicle>` 을 1개씩 순차 실행한다.
#      - 그룹별 `every` 값으로 실행 빈도를 조절한다. A=1(매 사이클), B=3(A 3회전당 1회), C=6(A 6회전당 1회).
#      - 사이클 번호는 상태 파일에 저장되므로 스케줄러를 재시작해도 B/C 위상이 초기화되지 않는다.
#   2) 트리거 큐(flow web → S3 → inbox)를 감시해, 요청이 들어오면 **정규 순회보다 먼저**
#      `python Main.py _TRIGGER_<vehicle>_<lot_id>_<step_id>` 로 해당 랏의 리포트를 즉시 발행한다.
#      - 요청은 durable active/history로 중복 차단하고, 결과 미확인 시 자동 재발송하지 않는다.
#      - 트리거 발행 메일은 config.yaml 의 email_receiver 를 무시하고, scheduler.yaml 의
#        `trigger.email_receiver` 그룹(허용목록 내)에만 발송한다(환경변수 AUTO_REPORT_EMAIL_RECEIVER 경유).
#   3) 랏 측정 history 를 web 이 읽어갈 수 있는 형태(JSON)로 outbox 에 내보낸다(S3 업로드는 범위 밖).
#
# 실행
#   python Scheduler.py                 # 상시 루프 (Ctrl+C 로 정지)
#   python Scheduler.py --once          # 사이클 1회만 수행하고 종료
#   python Scheduler.py --drain         # 대기 중인 트리거만 처리하고 종료
#   python Scheduler.py --status        # 현재 상태 요약 출력
#   python Scheduler.py --enqueue vehicle_A_A488GA.1_CC942300   # 트리거 수동 투입(테스트용)
#   python Scheduler.py --export-history                        # 랏 history outbox 재생성만 수행
#
# 주의
#   - Main.py 는 반드시 **별도 프로세스**로 실행한다. Main.py 는 병렬 차트 렌더링 워커가
#     __main__ 을 재import 하는 구조라, 스케줄러가 import 해서 main() 을 직접 호출하면 안 된다.
#   - 이 파일은 표준 라이브러리 + PyYAML 만 사용한다(무거운 pandas/duckdb import 없음).
# ==================================================================================================================================

import argparse
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import uuid
import time
from datetime import datetime, timedelta

import yaml
_runtime_zip = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'auto_report_runtime.zip')
if os.path.isfile(_runtime_zip) and _runtime_zip not in sys.path:
    sys.path.append(_runtime_zip)
from operator_console import color, plain

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'reformatter', 'scheduler.yaml')
VEHICLE_CONFIG_PATH = os.path.join(BASE_DIR, 'reformatter', 'config.yaml')

# 트리거 요청 값 검증 정규식.
#   lot_id / step_id 에 '_' 를 허용하지 않는 이유: Main.py 는 `_TRIGGER_{vehicle}_{lot}_{step}` 을
#   rsplit('_', 2) 로 되돌리므로, lot/step 에 '_' 가 들어가면 파싱이 어긋난다. (vehicle 은 '_' 허용)
_RE_VEHICLE = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
_RE_TOKEN = re.compile(r'^[A-Za-z0-9.-]{1,40}$')
_RE_USER = re.compile(r'^[A-Za-z0-9._-]{1,64}$')        # Main.py samsung_email 의 사용자 ID 규칙과 같다
_RE_REQUEST_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
REQUEST_KINDS = ('report', 'init_db', 'send_user')        # 큐 요청 종류 (기본 report)
QUEUE_MODES = ('TRIGGER', 'NORMAL', 'SINGLE', 'FORCE', 'ALL', 'DB_SETTING')

_STOP = threading.Event()        # SIGINT/SIGTERM 수신 플래그
_CURRENT_PROC = None             # 현재 실행 중인 Main.py 프로세스(정지 시 정리용)


# ==================================================================================================================================
# 기본 설정 (scheduler.yaml 이 없으면 이 내용으로 seed 생성)
# ==================================================================================================================================
_DEFAULT_CONFIG_YAML = """\
# ============================================================
# Auto Report Scheduler 설정 (scheduler.yaml)
# ============================================================
# 이 파일만 편집하면 순회 대상/주기/트리거 동작을 바꿀 수 있습니다(코드 수정 불필요).
# 제품(vehicle) 이름은 reformatter/config.yaml 에 정의된 키와 동일해야 합니다.
# ============================================================

scheduler:
  # ── 순회 주기 (Cycle timing, 단위: 초) ──
  cycle_idle_sec: 300          # 한 사이클(=그룹 A 1회전) 종료 후 대기 시간
  product_gap_sec: 5           # 제품 간 간격
  poll_interval_sec: 30        # 유휴 대기 중 트리거 큐 확인 주기
  main_timeout_sec: 10800      # Main.py 1회 최대 실행 시간(초). 초과 시 프로세스 트리 강제 종료
  execution_lock_wait_sec: 10800  # 다른 제품/Daily/ML 종료 대기 예산(실행 제한과 별도)
  python: ''                   # 비우면 스케줄러를 띄운 파이썬(sys.executable) 사용

  # ── 그룹 정의 (Groups) ──
  #   every: 이 그룹이 몇 사이클마다 1회 도는지. A=1(매번), B=3, C=6
  #   products: 그룹 안에서 순서대로 순회할 제품 리스트
  groups:
    - name: A
      every: 1
      products: ['vehicle_A']
    - name: B
      every: 3
      products: ['Vehicle_B']
    - name: C
      every: 6
      products: []

trigger:
  # ── 수동 트리거(강제발행) 큐 ──
  enabled: true
  queue_root: 'RUN/QUEUE'                       # 큐 관련 파일 루트
  inbox_dir: 'RUN/QUEUE/inbox'                  # flow web(S3 sync)이 요청 JSON 을 떨어뜨리는 폴더
  queue_file: 'RUN/QUEUE/trigger_queue.jsonl'   # 단일 파일 큐(append-only JSONL). inbox 와 병행 사용 가능
  max_per_check: 20                             # 1회 확인당 최대 처리 건수(폭주 방지)
  max_pending: 200                              # 메모리 대기열 상한; 나머지는 inbox/JSONL에 보관

  # 트리거 발행 메일 수신 그룹(= 메일링 xlsx 의 시트명). config.yaml 의 email_receiver 를 덮어쓴다.
  email_receiver: ['MANUAL_TRIGGER']
  # 요청 JSON 이 email_receiver 를 지정한 경우, 아래 허용목록에 있는 그룹만 받아들인다(외부 입력 검증).
  allowed_email_receiver: ['MANUAL_TRIGGER', 'POWER_USER', 'HOL']

  # 받아들일 요청 종류: report(리포트 발행) / init_db(DB 설치 = 최근 200일 적재) / send_user(개인 1명 발송)
  allowed_kinds: ['report', 'init_db', 'send_user']
  dedup_by_target: true        # 같은 vehicle/lot/step 은 req_id 가 달라도 재발행하지 않음(force:true 로 우회)
  max_retry: 0                 # 실패 시 재시도 횟수(0 = 재시도 없음 → '1회만 수행' 보장)
  retry_backoff_sec: 600
  history_keep: 2000           # 상태 파일에 보관할 처리 이력 건수

watchdog:
  # enabled/daily_time/recipients/mail_vehicle: My_config.py self.watchdog에서 지정
  stale_sec: 180
  progress_stale_sec: 1800
  poll_sec: 30
  immediate_alerts: false

# daily_trend/mlmode의 제품·수신처·시각·ML 경로: My_config.py에서 각각 별도 지정

lot_history:
  # ── 랏 측정 history 내보내기 (auto report → S3 → flow web) ──
  enabled: true
  out_dir: 'RUN/QUEUE/outbox'
  max_days: 30                 # 최근 N일 tkout_time 만 내보냄
"""


# ==================================================================================================================================
# 출력 / 로그
# ==================================================================================================================================
def _safe_text(msg):
    """콘솔 인코딩(cp949 등)에 없는 문자를 '?'로 치환한 문자열 반환."""
    enc = getattr(sys.stdout, 'encoding', None) or 'utf-8'
    try:
        return str(msg).encode(enc, errors='replace').decode(enc, errors='replace')
    except Exception:
        return str(msg)


_LOG_PATH = None
_LOG_LOCK = threading.Lock()


def log(msg, level='INFO'):
    """콘솔 + RUN/log/scheduler_log.txt 동시 기록 (30MB 초과 시 앞부분 잘라냄)."""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}"
    severity = {'ERROR':'error', 'WARN':'warn', 'OK':'ok'}.get(level,'info')
    print(color(_safe_text(line), severity), flush=True)
    if not _LOG_PATH:
        return
    with _LOG_LOCK:
        try:
            os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
            with open(_LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
            if os.path.getsize(_LOG_PATH) > 30 * 1024 * 1024:
                with open(_LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                    keep = f.read()[-24 * 1024 * 1024:]
                with open(_LOG_PATH, 'w', encoding='utf-8') as f:
                    f.write(keep)
        except Exception:
            pass   # 로그 실패로 스케줄러가 죽지 않도록


def log_child(msg):
    """Main.py 자식 프로세스의 출력 1줄을 그대로 전달(타임스탬프 중복 방지)."""
    print(_safe_text(msg), flush=True)
    if not _LOG_PATH:
        return
    with _LOG_LOCK:
        try:
            with open(_LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(plain(msg) + '\n')
        except Exception:
            pass


# ==================================================================================================================================
# 설정 로드
# ==================================================================================================================================

# Watchdog runs independently of the scheduler; foreground heartbeats prove loop progress.
# Watchdog은 운영 ledger 롤업(HTML/CSV)만 담당한다. 추세분석 임계값은 Daily Trend가 담당.
_WATCH_DEFAULTS = dict(enabled=True, daily_time='09:00', recipients=[], mail_vehicle='', ops_root='RUN/OPS',
                       poll_sec=30, stale_sec=180, progress_stale_sec=1800, immediate_alerts=False,
                       report_timeout_sec=900)
# 폴링 공통 상수
RETRY_THROTTLE_SEC = 300  # 발송 시도 후 최소 대기(스팸 방지)
INCIDENT_KEEP = 300  # watchdog_state.json에 보관할 상태 전이 최대 개수
_TREND_DEFAULTS = dict(enabled=False, daily_time='09:30', products=[], recipients=[], mail_vehicle='',
                       html_columns=2,
                       ml_table_dir='RUN/DB', ml_join_keys=['root_lot_id','wafer_id'],
                       ppt_max_bytes=10_000_000, html_max_bytes=2_000_000, mail_max_bytes=20_000_000,
                       chart_dpi=150, report_timeout_sec=3600, poll_sec=30,
                       min_lots=3, spec_out_pct=10, shift_sigma=2, shift_min_spec_frac=.02,
                       trend_correlation=.7)
_ML_DEFAULTS = dict(_TREND_DEFAULTS, daily_time='10:00',with_vehicle={},chart_dpi=150,
                    modules=['split_difference','time_trend','distribution_shift','spread_change','spike_rate','isolation_forest','local_outlier_factor'],
                    diagnostic_modules=['equipment_difference','spatial_pattern'],equipment_columns=[],
                    min_samples=12,min_lots=3,fdr_alpha=.05,spike_sigma=4.,rate_increase=.15,
                    rank_effect_min=.33,trend_min_correlation=.6,distribution_min_distance=.3,
                    analysis_seconds=120,analysis_max_tests=2000,max_group_pairs=64,
                    model_contamination=.05,model_max_samples=5000,random_state=42,
                    influence_enabled=True,influence_columns=[],influence_exclude_columns=[],
                    influence_max_columns=80,influence_top_k=6,influence_min_lots=8,
                    influence_max_categories=12,influence_min_coverage=.5,
                    influence_min_effect=.3,influence_fdr_alpha=.05,influence_permutations=999,
                    influence_seconds=60,influence_max_tests=240,influence_max_join_rows=200000,
                    influence_max_wafers_per_root=100,influence_min_matched_roots=5,
                    influence_similar_mismatch=.15,influence_min_balance=.5)
_HEART = {}
_ACTIVE_CONFIG = None
_LOCK_FILES=[]


def _atomic_json_file(path, value):
    os.makedirs(os.path.dirname(path),exist_ok=True)
    temp=path+'.'+uuid.uuid4().hex+'.tmp'
    try:
        with open(temp,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.remove(temp)


def _read_json(path, fallback=None):
    try:
        with open(path,encoding='utf-8') as stream:return json.load(stream)
    except (OSError,ValueError):return fallback


def _ops_root(cfg):
    return _abspath(cfg.get('watchdog',{}).get('ops_root','RUN/OPS'))


def heartbeat(cfg, phase=None, **values):
    global _HEART
    if phase is not None:_HEART['phase']=phase
    _HEART.update(values)
    _HEART.update(pid=os.getpid(),updated=time.time(),updated_at=datetime.now().isoformat(timespec='seconds'))
    _atomic_json_file(os.path.join(_ops_root(cfg),'scheduler_heartbeat.json'),_HEART)


def watchdog_health(cfg, beat, now=None):
    now=time.time() if now is None else now
    if not beat:return dict(state='missing',message='제품 순회 상태 확인 불가',detail='Scheduler 시작 이력을 확인할 수 없습니다.')
    last=float(beat.get('updated',0));phase=beat.get('phase','unknown')
    detail=f"마지막 loop 응답: {datetime.fromtimestamp(last)} / 단계: {phase} / 제품: {beat.get('vehicle','-')}"
    if phase=='stopped':
        return dict(state='stopped',message='Scheduler loop 정지',detail=detail+' / 종료 상태가 기록되었습니다.')
    if now-last>float(cfg['watchdog']['stale_sec']):
        return dict(state='stale',message='Scheduler loop 정지 의심',detail=detail+
                    f' / {now-last:.0f}초 무응답. 정확한 중단 시점은 마지막 응답과 최초 감지 사이입니다.')
    if phase=='main':
        progress=_read_json(os.path.join(_ops_root(cfg),'progress',str(beat.get('run_id'))+'.json'),{})
        if progress:
            age=now-float(progress.get('updated',now))
            detail+=f" / Main 단계: {progress.get('stage')} / 마지막 단계 갱신: {datetime.fromtimestamp(progress.get('updated',now))}"
            if (progress.get('stage') == 'waiting_execution' and
                    age <= max(0, float(cfg['scheduler'].get('execution_lock_wait_sec', 10800))) + 30):
                return dict(state='healthy', message='공통 실행 잠금 대기', detail=detail)
            if age>float(cfg['watchdog']['progress_stale_sec']):
                return dict(state='slow',message='Scheduler 동작 중 / Main 처리 지연',detail=detail+f' / {age:.0f}초 단계 갱신 없음')
    return dict(state='healthy',message='Scheduler 정상 동작 중',detail=detail)


def start_watchdog(cfg, config_path):
    if not cfg['watchdog']['enabled']:return
    path=os.path.join(_ops_root(cfg),'watchdog.lock')
    lock=_read_json(path,{})
    if lock.get('pid') and _pid_alive(int(lock['pid'])):return
    out_path=os.path.join(_ops_root(cfg),'watchdog_process.log')
    os.makedirs(os.path.dirname(out_path),exist_ok=True)
    with open(out_path,'a',encoding='utf-8') as output:
        options=dict(cwd=BASE_DIR,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,close_fds=True)
        if os.name=='nt':
            options['creationflags']=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:options['start_new_session']=True
        subprocess.Popen([sys.executable,os.path.abspath(__file__),'--watchdog','--config',os.path.abspath(config_path)],**options)
    log(f"운영 감시 시작 | 매일 {cfg['watchdog']['daily_time']} 상태 보고 예약 | 수신 그룹: {', '.join(cfg['watchdog']['recipients']) or '(없음)'}", 'OK')


def run_watchdog(cfg, config_path, once=False, preview=False):
    root=_ops_root(cfg);os.makedirs(root,exist_ok=True)
    if not acquire_lock(os.path.join(root,'watchdog.lock')):return 1
    state_path=os.path.join(root,'watchdog_state.json')
    state=_read_json(state_path,dict(incidents=[]))
    while not _STOP.is_set():
        # Hot reload addresses/time/thresholds without restarting the monitor.
        cfg=load_config(config_path,create_if_missing=False,strict_services=False)
        settings=cfg['watchdog'];now=time.time()
        if settings.get('_config_error'):
            log('Watchdog 설정 오류: '+settings['_config_error'],'ERROR');return 1
        health=watchdog_health(cfg,_read_json(os.path.join(root,'scheduler_heartbeat.json')),now)
        if health['state']!=state.get('health_state'):
            incident=dict(at=now,state=health['state'],message=health['message'],detail=health['detail'])
            state.setdefault('incidents',[]).append(incident)
            state['incidents']=state['incidents'][-INCIDENT_KEEP:]
            state['health_state']=health['state'];state['transition_at']=now
            state['alert_pending']=health['state']!='healthy' or bool(state.get('had_problem'))
            state['had_problem']=health['state']!='healthy'
        today=datetime.fromtimestamp(now).strftime('%Y-%m-%d')
        clock=datetime.fromtimestamp(now).strftime('%H:%M')
        daily=clock>=settings['daily_time'] and state.get('last_daily_date')!=today
        immediate=bool(settings['immediate_alerts'] and state.get('alert_pending'))
        can_publish=bool(settings['enabled'] and settings['recipients'])
        if settings.get('enabled') and not settings.get('recipients'):
            if not state.get('_recipients_warned'):
                log('Watchdog 활성화됐으나 recipients가 비어 미리보기만 저장합니다.', 'WARN')
                state['_recipients_warned']=True
        elif settings.get('recipients'):
            state.pop('_recipients_warned', None)
        if not can_publish:daily=False;immediate=False
        if now-state.get('last_attempt_at',0)<RETRY_THROTTLE_SEC:daily=False;immediate=False
        report_rc=0
        if preview or (can_publish and (once or daily or immediate)):
            kind='daily' if daily or preview or once else 'alert'
            report_id=f'{kind}-{today}' if kind=='daily' else f'alert-{int(state["transition_at"])}'
            if preview:report_id='preview-'+datetime.now().strftime('%Y%m%d-%H%M%S')
            effective=dict(settings)
            effective['mail_vehicle']=settings.get('mail_vehicle') or next(iter(all_products(cfg)),'')
            effective['products']=list(all_products(cfg))
            since=state.get('last_daily_at',now-86400)
            incidents=[i for i in state.get('incidents',[]) if i['at']>=since]
            report_health=dict(health)
            if incidents:
                report_health['detail']+=' / 감시 구간 이력: '+'; '.join(f"{datetime.fromtimestamp(i['at'])} {i['message']} ({i['detail']})" for i in incidents)
            request=dict(id=report_id,settings=effective,health=report_health,now=now,window_start=since,
                         send=bool(settings['enabled'] and settings['recipients'] and not preview))
            request_path=os.path.join(root,'watchdog_request.json');_atomic_json_file(request_path,request)
            log_path=os.path.join(root,'watchdog',report_id+'.log')
            try:
                os.makedirs(os.path.dirname(log_path),exist_ok=True)
                with open(log_path,'a',encoding='utf-8') as _logf:
                    _logf.write(f"[{datetime.now().isoformat(timespec='seconds')}] watchdog {report_id} start send={request['send']}\n")
                    _logf.flush()
                    report_rc=_run_service_main(cfg,['--watchdog-report',request_path],
                                               int(settings['report_timeout_sec']),output=_logf,heavy=False)
                    _logf.write(f"[{datetime.now().isoformat(timespec='seconds')}] watchdog {report_id} rc={report_rc}\n")
                if report_rc==0:
                    if request['send']:
                        if kind=='daily':state.update(last_daily_date=today,last_daily_at=now)
                        state['alert_pending']=False
                        log(f'운영 감시 보고서 발행 완료 | {report_id}', 'OK')
                    else:
                        log(f'운영 감시 보고서 미리보기 저장 | 메일 발송 없음 | {report_id}', 'OK')
                        # Do not mark unsent reports delivered. Avoid rebuilding every poll.
                        state['last_preview_date']=today
                else:
                    log(f'Watchdog 보고서 생성/발송 실패 rc={report_rc} ({report_id}); 로그: {log_path}','ERROR')
            except (subprocess.TimeoutExpired,OSError) as exc:
                report_rc=1;log(f'Watchdog 보고서 프로세스 실패 ({report_id}): {type(exc).__name__}','ERROR')
            state['last_attempt_at']=now
        state['updated']=now;_atomic_json_file(state_path,state)
        if once or preview:return report_rc
        _STOP.wait(max(5,float(settings['poll_sec'])))
    return 0


def start_daily_trend(cfg, config_path, service='daily_trend'):
    settings=cfg[service]
    if not settings['enabled'] or not settings['products'] or not settings['recipients']:return
    root=_ops_root(cfg);os.makedirs(root,exist_ok=True)
    lock=_read_json(os.path.join(root,service+'.lock'),{})
    if lock.get('pid') and _pid_alive(int(lock['pid'])):return
    with open(os.path.join(root,service+'_process.log'),'a',encoding='utf-8') as output:
        options=dict(cwd=BASE_DIR,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,close_fds=True)
        if os.name=='nt':
            options['creationflags']=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:options['start_new_session']=True
        subprocess.Popen([cfg['scheduler'].get('python') or sys.executable,os.path.abspath(__file__),
                          '--'+service.replace('_','-'),'--config',os.path.abspath(config_path)],**options)
    name='일일 측정 추세' if service=='daily_trend' else '통계·ML 이상 탐지'
    log(f"{name} 시작 | 매일 {settings['daily_time']} 발행 예약 | 대상 제품: {', '.join(settings['products'])} | 수신 그룹: {', '.join(settings['recipients'])}", 'OK')


def _service_due(settings, state, now, today, clock):
    """일일 발행 시점 판정(공통 헬퍼).

    daily_time 이후 + 오늘 미발행 + 재시도 쿨다운 경과를 모두 만족해야 True.
    """
    if not (settings.get('enabled') and settings.get('recipients')):
        return False
    if not (clock>=settings['daily_time'] and state.get('last_daily_date')!=today):
        return False
    if settings.get('recipients') or state.get('last_preview_date')!=today:
        pass
    else:
        return False
    return now-state.get('last_attempt_at',0)>=RETRY_THROTTLE_SEC


def run_daily_trend(cfg, config_path, once=False, preview=False, service='daily_trend'):
    """Independent timer: chart generation cannot delay watchdog heartbeat checks."""
    root=_ops_root(cfg);os.makedirs(root,exist_ok=True)
    if not acquire_lock(os.path.join(root,service+'.lock')):return 1
    state_path=os.path.join(root,service+'_state.json')
    state=_read_json(state_path,{})
    while not _STOP.is_set():
        cfg=load_config(config_path,create_if_missing=False,strict_services=False);settings=cfg[service]
        if settings.get('_config_error'):
            log(service+' 설정 오류: '+settings['_config_error'],'ERROR');return 1
        now=time.time();today=datetime.fromtimestamp(now).strftime('%Y-%m-%d')
        clock=datetime.fromtimestamp(now).strftime('%H:%M')
        can_publish=bool(settings['enabled'] and settings['recipients'])
        due=_service_due(settings,state,now,today,clock)
        rc=0
        if (preview or (can_publish and (once or due))) and settings['products']:
            # Resume the same artifacts/part IDs after a restart or partial delivery.
            report_id=('preview-'+uuid.uuid4().hex) if preview else 'daily-'+today
            request=dict(id=report_id,settings=settings,now=now,service=service,
                         send=bool(settings['enabled'] and settings['recipients'] and not preview))
            request_path=os.path.join(root,service+'_request.json');_atomic_json_file(request_path,request)
            try:
                rc=_run_service_main(cfg,['--daily-trend-report',request_path],int(settings['report_timeout_sec']))
            except subprocess.TimeoutExpired:
                rc=1;log(f'{service} 시간 초과','ERROR')
            except OSError as exc:
                rc=1;log(f'{service} 실행 실패: {exc}','ERROR')
            if rc==0:
                state['last_daily_date' if request['send'] else 'last_preview_date']=today
            else:log(f'{service} 실패 rc={rc}; 완료된 메일은 재발송하지 않습니다','ERROR')
            state.update(last_attempt_at=now,last_result=rc)
            _atomic_json_file(state_path,state)
        elif (preview or (can_publish and once)) and not settings['products']:
            log(f'발행 대상 제품이 없습니다. My_config.py의 {service}.products 또는 scheduler.yaml 제품 그룹을 확인하세요.','ERROR');rc=1
        if once or preview:return rc
        _STOP.wait(max(5,float(settings['poll_sec'])))
    return 0


def start_background_services(cfg, config_path):
    """Optional service startup failures must not abort the Auto Report product cycle."""
    for service in ('watchdog','daily_trend','mlmode'):
        try:
            if service=='watchdog':start_watchdog(cfg,config_path)
            else:start_daily_trend(cfg,config_path,service)
        except Exception as exc:
            name={'watchdog':'운영 감시','daily_trend':'일일 측정 추세','mlmode':'통계·ML 이상 탐지'}[service]
            log(f'{name} 시작 실패; 제품 순회는 계속됩니다: {type(exc).__name__}: {exc}','ERROR')


def _service_settings(service, defaults, yaml_settings, service_config):
    import math
    settings=dict(defaults)
    settings.update(yaml_settings or {})
    settings.update(getattr(service_config,service,{}) or {})
    list_keys=('recipients',) if service=='watchdog' else ('products','recipients','ml_join_keys')
    for key in list_keys:
        if isinstance(settings[key],str):settings[key]=[v.strip() for v in settings[key].split(',') if v.strip()]
        if not isinstance(settings[key],list) or any(not isinstance(v,str) for v in settings[key]):
            raise ValueError(service+'.'+key+'는 문자열 목록이어야 합니다')
    if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',str(settings['daily_time'])):
        raise ValueError(service+'.daily_time은 HH:MM 형식이어야 합니다')
    for key in ('poll_sec','report_timeout_sec'):
        value=float(settings[key])
        if not math.isfinite(value) or value<=0:raise ValueError(service+'.'+key+'는 유한한 양수여야 합니다')
    if int(settings['report_timeout_sec'])<=0:raise ValueError(service+'.report_timeout_sec는 1초 이상이어야 합니다')
    if service=='watchdog':return settings
    settings['products']=list(dict.fromkeys(settings['products']))
    for key in ('ppt_max_bytes','html_max_bytes','mail_max_bytes'):
        if int(settings[key])<=0:raise ValueError(service+'.'+key+'는 양수여야 합니다')
    if int(settings['chart_dpi'])<120:raise ValueError(service+'.chart_dpi는 가독성을 위해 120 이상이어야 합니다')
    if service=='mlmode':
        for key in ('analysis_seconds','analysis_max_tests','max_group_pairs'):
            if not math.isfinite(float(settings[key])) or float(settings[key])<1:raise ValueError('mlmode.'+key+'는 1 이상의 유한한 값이어야 합니다')
        for key in ('rank_effect_min','trend_min_correlation','distribution_min_distance'):
            if not 0<float(settings[key])<=1:raise ValueError('mlmode.'+key+'는 0~1 범위여야 합니다')
        if not isinstance(settings['with_vehicle'],dict):raise ValueError('mlmode.with_vehicle는 제품별 목록의 dict여야 합니다')
        if not 0<float(settings['model_contamination'])<=.5:raise ValueError('mlmode.model_contamination은 0~0.5 범위여야 합니다')
        if not 0<float(settings['fdr_alpha'])<1:raise ValueError('mlmode.fdr_alpha는 0~1 범위여야 합니다')
        if not 0<float(settings['rate_increase'])<=1:raise ValueError('mlmode.rate_increase는 0~1 범위여야 합니다')
        if int(settings['model_max_samples'])<12:raise ValueError('mlmode.model_max_samples는 12 이상이어야 합니다')
        for key in ('influence_seconds','influence_max_tests','influence_max_join_rows','influence_max_columns','influence_top_k'):
            if not math.isfinite(float(settings.get(key,1))) or float(settings.get(key,1))<1:raise ValueError('mlmode.'+key+'는 1 이상의 유한한 값이어야 합니다')
        for key in ('influence_fdr_alpha','influence_min_coverage'):
            if not 0<float(settings.get(key,.05))<=1:raise ValueError('mlmode.'+key+'는 0 초과 1 이하 범위여야 합니다')
        if not 0<float(settings.get('influence_min_effect',.3)):raise ValueError('mlmode.influence_min_effect는 양수여야 합니다')
    return settings


def load_config(path=CONFIG_PATH, create_if_missing=True, strict_services=True):
    """scheduler.yaml 로드. 없으면 기본 설정으로 seed 파일을 만들고 그 내용을 반환한다."""
    if not os.path.exists(path) and create_if_missing:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(_DEFAULT_CONFIG_YAML)
        log(f"scheduler.yaml 이 없어 기본값으로 생성했습니다: {path}")

    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = yaml.safe_load(_DEFAULT_CONFIG_YAML)

    cfg.setdefault('scheduler', {})
    cfg.setdefault('trigger', {})
    cfg.setdefault('lot_history', {})
    from My_config import GLOBAL_CONFIG as service_config
    for service,defaults in [('watchdog',_WATCH_DEFAULTS),('daily_trend',_TREND_DEFAULTS),('mlmode',_ML_DEFAULTS)]:
        raw=cfg.get(service)
        try:
            cfg[service]=_service_settings(service,defaults,raw,service_config)
            if service != 'watchdog' and not cfg[service]['products']:
                cfg[service]['products'] = list(dict.fromkeys(
                    product for _name, _every, products in _groups(cfg) for product in products))
        except (ValueError,TypeError,KeyError,OverflowError) as exc:
            if strict_services:raise
            # Keep the shared ledger location even when only Watchdog settings are invalid.
            fallback=dict(defaults,enabled=False,recipients=[],_config_error=str(exc))
            if service=='watchdog':
                for source in (raw,getattr(service_config,service,{})):
                    if isinstance(source,dict) and source.get('ops_root'):fallback['ops_root']=source['ops_root']
            cfg[service]=fallback
            log(f'{service} 설정 오류로 해당 서비스 비활성화; Auto Report 순회 계속: {exc}','ERROR')
    return cfg


def known_vehicles():
    """reformatter/config.yaml 에 정의된 제품(vehicle) 키 집합."""
    try:
        with open(VEHICLE_CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        return {k for k in data.keys() if isinstance(data.get(k), dict)}
    except Exception as e:
        log(f"config.yaml 읽기 실패 ({e}) → 수동 요청의 제품을 확인할 수 없습니다.", 'ERROR')
        return set()


def _abspath(rel):
    """설정의 상대 경로를 스케줄러 위치 기준 절대 경로로."""
    return rel if os.path.isabs(rel) else os.path.join(BASE_DIR, rel.replace('/', os.sep))


# ==================================================================================================================================
# 상태 저장소 — 사이클 번호 / 큐 오프셋 / 대기 요청 / 처리 이력
# ==================================================================================================================================
class State:
    """RUN/QUEUE/scheduler_state.json 한 파일에 스케줄러 영속 상태를 보관한다.

    구조
      cycle         : 지금까지 완료한 사이클 수(그룹 every 판정 기준)
      queue_offset  : trigger_queue.jsonl 을 어디까지 읽었는지(바이트)
      pending       : 아직 처리하지 못한 트리거 요청 리스트(재시도 대기 포함)
      history       : dedup_key → 처리 결과(1회 실행 보장의 근거)
      done_targets  : 'vehicle|lot|step' → 발행 시각 (req_id 가 달라도 재발행 차단)
    """

    def __init__(self, path):
        self.path = path
        self.data = {'cycle': 0, 'queue_offset': 0, 'pending': [], 'active': None,
                     'history': {}, 'done_targets': {}}
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict) or not isinstance(loaded.get('pending', []), list):
                raise ValueError('상태 스키마 오류')
            if any(not isinstance(loaded.get(key, {}), dict) for key in ('history', 'done_targets')):
                raise ValueError('상태 이력 스키마 오류')
            self.data.update(loaded)
        except Exception as e:
            raise RuntimeError(f'상태 파일을 읽을 수 없어 중복 실행 방지를 위해 중단합니다: {self.path} ({e})') from e

    def save(self):
        """임시 파일 → os.replace 로 원자적 저장(중간에 죽어도 파일이 깨지지 않음)."""
        # A failed durable claim must stop execution before Main can send mail.
        _atomic_json_file(self.path, self.data)

    # ── 편의 접근자 ──
    @property
    def cycle(self):
        return int(self.data.get('cycle', 0))

    @cycle.setter
    def cycle(self, v):
        self.data['cycle'] = int(v)

    def prune_history(self, keep):
        hist = self.data.get('history', {})
        if len(hist) <= keep:
            return
        # finished_at(없으면 started_at) 최신순으로 keep 건만 유지
        items = sorted(hist.items(),
                       key=lambda kv: kv[1].get('finished_at') or kv[1].get('started_at') or '',
                       reverse=True)
        self.data['history'] = dict(items[:keep])


# ==================================================================================================================================
# 트리거 큐 — 요청 수집(inbox 폴더 + JSONL 파일) / 정규화 / 검증
# ==================================================================================================================================
def _norm_request(raw, source):
    """요청 dict 를 표준 형태로 정규화. 실패 시 (None, 사유) 반환.

    허용 입력
      {"vehicle": "...", "lot_id": "...", "step_id": "..."}   ← 권장
      {"key": "vehicle_A_A488GA.1_CC942300"}                  ← 제품_lotid_stepid 단일 키
    선택 필드: req_id, email_receiver, requested_by, requested_at, note, force
    """
    if not isinstance(raw, dict):
        return None, 'JSON 객체가 아님'
    req_id = str(raw.get('req_id') or '').strip()
    if req_id and not _RE_REQUEST_ID.fullmatch(req_id):
        return None, 'req_id는 영숫자로 시작하는 영숫자/./_/- 128자 이하여야 합니다'
    if not isinstance(raw.get('force', False), bool):
        return None, 'force는 true/false만 허용됩니다'

    kind = str(raw.get('kind') or 'report').strip().lower()
    if kind not in REQUEST_KINDS:
        return None, f"kind는 {'/'.join(REQUEST_KINDS)}만 허용됩니다"
    if str(raw.get('mode') or '').upper() == 'DB_SETTING':
        if kind == 'send_user':
            return None, 'DB setting 적재는 개인 발송과 함께 사용할 수 없습니다'
        kind = 'init_db'
    vehicle = str(raw.get('vehicle') or '').strip()
    if kind == 'init_db':
        # DB setting은 Lot/Step 없이 제품·기간·병렬 조회 상한만 지정한다.
        if not _RE_VEHICLE.match(vehicle):
            return None, f'vehicle 형식 오류: {vehicle!r}'
        options = {}
        for key in ('days', 'parallel'):
            if key in raw:
                if type(raw[key]) is not int or raw[key] < 1:
                    return None, f'{key}는 1 이상의 정수여야 합니다'
                options[key] = raw[key]
        return {
            'req_id': str(raw.get('req_id') or '').strip(), 'kind': 'init_db',
            'mode': 'INIT_DB', 'generate_only': True,
            'vehicle': vehicle, 'lot_id': '', 'step_id': '', 'email_receiver': None, 'send_user': '',
            'requested_by': str(raw.get('requested_by') or '').strip(),
            'requested_at': str(raw.get('requested_at') or '').strip(),
            'note': str(raw.get('note') or '').strip(), 'force': bool(raw.get('force', False)),
            'source': source, 'attempts': 0, 'next_attempt_ts': 0,
            **options,
        }, ''
    if 'days' in raw or 'parallel' in raw:
        return None, 'days/parallel은 DB setting 적재에서만 사용합니다'
    lot_id = str(raw.get('lot_id') or raw.get('lot') or '').strip()
    step_id = str(raw.get('step_id') or raw.get('step') or '').strip()
    key = str(raw.get('key') or '').strip()

    if key and not (vehicle and lot_id and step_id):
        # 제품명에 '_' 가 있을 수 있으므로 뒤에서 2번 분해 (Main.py 와 동일 규칙)
        parts = key.rsplit('_', 2)
        if len(parts) != 3:
            return None, f"key 형식 오류(제품_lotid_stepid): {key!r}"
        vehicle, lot_id, step_id = parts[0].strip(), parts[1].strip(), parts[2].strip()

    if not (vehicle and lot_id and step_id):
        return None, 'vehicle/lot_id/step_id 누락'
    mode = str(raw.get('mode') or 'TRIGGER').upper()
    if mode not in QUEUE_MODES:
        return None, f"관리 큐 mode는 {'/'.join(QUEUE_MODES)}만 허용됩니다"
    generate_only = raw.get('generate_only', False)
    if not isinstance(generate_only, bool):
        return None, 'generate_only는 true/false만 허용됩니다'
    send_user = str(raw.get('send_user') or '').strip()
    if kind == 'send_user':
        # 개인 발송 = Main.py --send-user USER --prime-key … (메일링 엑셀 없이 1명). USER 는 도메인 없는 ID.
        if not _RE_USER.match(send_user):
            return None, f'send_user는 도메인 없는 사용자 ID여야 합니다: {send_user!r}'
        if mode not in ('TRIGGER', 'SINGLE'):
            return None, '개인 발송은 TRIGGER/SINGLE 분석 범위만 지원합니다'
        if generate_only:
            return None, '개인 발송은 생성 전용과 함께 쓸 수 없습니다'
    elif send_user:
        return None, "send_user는 kind='send_user' 요청에서만 사용합니다"
    if not _RE_VEHICLE.match(vehicle):
        return None, f'vehicle 형식 오류: {vehicle!r}'
    # 쉼표로 여러 Lot/Step 을 한 요청에 담을 수 있다(Main.py 가 데이터를 한 번만 읽어 Lot 별로 발행).
    try:
        pairs = _pairs(lot_id, step_id)
    except ValueError as exc:
        return None, str(exc)
    lot_id, step_id = ','.join(p[0] for p in pairs), ','.join(p[1] for p in pairs)

    recv = raw.get('email_receiver')
    if isinstance(recv, str):
        recv = [r.strip() for r in recv.split(',') if r.strip()]
    elif isinstance(recv, (list, tuple)):
        if any(not isinstance(r, str) for r in recv):
            return None, 'email_receiver는 문자열 또는 문자열 목록이어야 합니다'
        recv = [r.strip() for r in recv if r.strip()]
    elif recv is None:
        recv = None
    else:
        return None, 'email_receiver는 문자열 또는 문자열 목록이어야 합니다'

    req = {
        'req_id': str(raw.get('req_id') or '').strip(),
        'kind': kind, 'send_user': send_user,
        'mode': mode, 'generate_only': generate_only,
        'vehicle': vehicle,
        'lot_id': lot_id,
        'step_id': step_id,
        'email_receiver': recv,
        'requested_by': str(raw.get('requested_by') or '').strip(),
        'requested_at': str(raw.get('requested_at') or '').strip(),
        'note': str(raw.get('note') or '').strip(),
        'force': bool(raw.get('force', False)),
        'source': source,
        'attempts': 0,
        'next_attempt_ts': 0,
    }
    return req, ''


def _dedup_key(req):
    """1회 실행 보장의 기준 키. req_id 가 있으면 그것을, 없으면 대상 조합을 쓴다."""
    if req.get('req_id'):
        return 'id:' + req['req_id']
    return 'tgt:' + target_key(req)


def target_key(req):
    if req.get('kind') == 'init_db':
        return f"{req['vehicle']}|INIT_DB"
    return f"{req['vehicle']}|{req['lot_id']}|{req['step_id']}"


def _pairs(lot, step, limit=100):
    """'L1,L2' + 'S1' 같은 쉼표 목록 → [(Lot, Step)]. My_Function.trigger_pairs 와 같은 규칙.
    같은 개수면 순서대로 짝, 한쪽이 1개면 공통 적용. 대상은 늘 짝 목록(같은 길이)으로 정규화해 쓴다."""
    lots = [v.strip() for v in str(lot).split(',') if v.strip()]
    steps = [v.strip() for v in str(step).split(',') if v.strip()]
    if not lots or not steps:
        raise ValueError('vehicle/lot_id/step_id 누락')
    bad = [v for v in lots + steps if not _RE_TOKEN.match(v)]
    if bad:
        raise ValueError(f"lot_id/step_id 형식 오류(영숫자/./- 만, '_' 불가): {bad[:3]!r}")
    if len(lots) == len(steps):
        pairs = list(zip(lots, steps))
    elif len(steps) == 1:
        pairs = [(v, steps[0]) for v in lots]
    elif len(lots) == 1:
        pairs = [(lots[0], v) for v in steps]
    else:
        raise ValueError(f'Lot {len(lots)}개와 Step {len(steps)}개를 짝지을 수 없습니다')
    pairs = list(dict.fromkeys(pairs))
    if len(pairs) > limit:
        raise ValueError(f'한 요청에 최대 {limit}개 대상')
    return pairs


def main_arguments(req, recv):
    """대기열 요청 1건 → Main.py의 보고서/DB setting 명령 인자 목록."""
    vehicle, lot, step = req['vehicle'], req.get('lot_id', ''), req.get('step_id', '')
    kind, mode = req.get('kind', 'report'), req.get('mode', 'TRIGGER')
    if kind == 'init_db':
        args = ['--init-db', vehicle]
        for key in ('days', 'parallel'):
            if key in req:
                args.extend(['--' + key, str(req[key])])
        return args
    if kind == 'send_user':
        return ['--send-user', req['send_user'], '--prime-key', f'{vehicle}_{lot}_{step}'] + (
            ['--single'] if mode == 'SINGLE' else [])
    if mode in ('FORCE', 'ALL'):
        # FORCE/ALL 은 Main.py 가 인자 안의 수신 그룹 1개로만 보낸다(_TRIGGER_FORCE_<group>_<v>_<l>_<s>).
        group = (recv or ['NONE'])[0]
        return [f'_TRIGGER_{mode}_{group}_{vehicle}_{lot}_{step}']
    prefix = '_TRIGGER_' if mode == 'TRIGGER' else f'_TRIGGER_{mode}_'
    return [prefix + f'{vehicle}_{lot}_{step}']


def collect_requests(cfg, state):
    """inbox 폴더와 JSONL 큐에서 새 요청을 읽어 state['pending'] 에 넣는다. 새로 담은 건수 반환."""
    tcfg = cfg['trigger']
    added = 0
    max_pending = max(1, int(tcfg.get('max_pending', 200)))

    inbox = _abspath(tcfg.get('inbox_dir', 'RUN/QUEUE/inbox'))
    done_dir = os.path.join(os.path.dirname(inbox), 'done')
    fail_dir = os.path.join(os.path.dirname(inbox), 'failed')
    for d in (inbox, done_dir, fail_dir):
        os.makedirs(d, exist_ok=True)

    # ── ① inbox 폴더: 파일 1개 = 요청 1건 (S3 오브젝트 1:1 대응) ──
    #    쓰는 쪽은 '.tmp 로 쓴 뒤 .json 으로 rename' 규칙을 지켜야 한다(부분 기록 방지).
    try:
        names = sorted(n for n in os.listdir(inbox) if n.lower().endswith('.json'))
    except Exception as e:
        log(f"inbox 읽기 실패: {e}", 'WARN')
        names = []

    for name in names:
        if len(state.data.get('pending', [])) >= max_pending:
            break
        fpath = os.path.join(inbox, name)
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                raw = json.load(f)
        except Exception as e:
            log(f"트리거 파일 파싱 실패 → failed 이동: {name} ({e})", 'WARN')
            _move(fpath, os.path.join(fail_dir, name))
            continue

        req, why = _norm_request(raw, f'inbox:{name}')
        if req is None:
            log(f"트리거 요청 무효 → failed 이동: {name} ({why})", 'WARN')
            _move(fpath, os.path.join(fail_dir, name))
            continue

        req['_inbox_file'] = fpath
        req['_done_path'] = os.path.join(done_dir, name)
        req['_fail_path'] = os.path.join(fail_dir, name)
        if _enqueue(state, req, cfg):
            added += 1
        else:
            # Repeated polling must not move a queued request to done before it runs.
            waiting = list(state.data.get('pending', []))
            active = state.data.get('active') or {}
            if active.get('request'):waiting.append(active['request'])
            if not any(p.get('_inbox_file') == fpath for p in waiting):
                _move(fpath, os.path.join(done_dir, name))

    # ── ② JSONL 단일 파일 큐: append-only, 읽은 위치(byte offset)를 상태에 저장 ──
    qfile = _abspath(tcfg.get('queue_file', 'RUN/QUEUE/trigger_queue.jsonl'))
    if os.path.exists(qfile):
        try:
            size = os.path.getsize(qfile)
            offset = int(state.data.get('queue_offset', 0))
            if size < offset:      # 파일이 잘리거나 새로 만들어진 경우 → 처음부터
                log('트리거 JSONL 이 축소됨 → 오프셋 초기화', 'WARN')
                offset = 0
            if size > offset:
                with open(qfile, 'rb') as f:
                    f.seek(offset)
                    while len(state.data.get('pending', [])) < max_pending:
                        position = f.tell()
                        line = f.readline()
                        if not line or not line.endswith(b'\n'):
                            break  # Incomplete UTF-8/JSON remains unread at the same byte offset.
                        state.data['queue_offset'] = f.tell()
                        line = line.decode('utf-8', errors='replace').strip()
                        if not line or line.startswith('#'):continue
                        try:raw = json.loads(line)
                        except ValueError:raw = {'key': line}
                        req, why = _norm_request(raw, f'jsonl:{position}')
                        if req is None:
                            log(f"트리거 JSONL 라인 무효: {line[:120]} ({why})", 'WARN');continue
                        if _enqueue(state, req, cfg):added += 1
        except Exception as e:
            log(f"트리거 JSONL 읽기 실패: {e}", 'WARN')

    if added or os.path.exists(qfile):
        state.save()
    return added


def _enqueue(state, req, cfg):
    """중복이 아니면 pending 에 추가하고 True. 이미 처리한 요청이면 False."""
    tcfg = cfg['trigger']
    dk = _dedup_key(req)

    if dk in state.data.get('history', {}):
        log(f"트리거 중복(이미 처리) 무시: {dk} ({target_key(req)})")
        return False

    if tcfg.get('dedup_by_target', True) and not req.get('force'):
        done = state.data.get('done_targets', {})
        if req.get('kind') == 'init_db':
            prev = done.get(target_key(req))
            if prev:
                log(f"트리거 중복(이미 처리 {prev}) 무시: {target_key(req)}")
                return False
        else:
            # 여러 대상 요청이면 이미 발행된 대상만 빼고 나머지는 처리한다.
            pairs = _pairs(req['lot_id'], req['step_id'])
            left = [p for p in pairs if not done.get(f"{req['vehicle']}|{p[0]}|{p[1]}")]
            if not left:
                log(f"트리거 중복(같은 lot/step 이미 발행) 무시: {target_key(req)}")
                return False
            if len(left) < len(pairs):
                log(f"이미 발행된 대상 {len(pairs) - len(left)}건 제외: {target_key(req)}")
                req['lot_id'], req['step_id'] = ','.join(p[0] for p in left), ','.join(p[1] for p in left)

    waiting = list(state.data.get('pending', []))
    active = state.data.get('active') or {}
    if active.get('request'):waiting.append(active['request'])
    for p in waiting:
        if _dedup_key(p) == dk or target_key(p) == target_key(req):
            log(f"트리거 중복(대기열에 존재) 무시: {target_key(req)}")
            return False

    state.data.setdefault('pending', []).append(req)
    log(f"트리거 접수: {target_key(req)} "
        f"(req_id={req['req_id'] or '-'}, by={req['requested_by'] or '-'}, src={req['source']})")
    return True


def _move(src, dst):
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            root, ext = os.path.splitext(dst)
            dst = f"{root}.{int(time.time())}{ext}"
        os.replace(src, dst)
    except Exception as e:
        log(f"파일 이동 실패 {src} → {dst}: {e}", 'WARN')


# ==================================================================================================================================
# Main.py 실행기
# ==================================================================================================================================
def _run_service_main(cfg, args, timeout, output=None, heavy=True):
    """Services share shutdown/tree cleanup, but keep their own timer and log."""
    global _CURRENT_PROC
    wait_budget = max(0, int(cfg['scheduler'].get('execution_lock_wait_sec', 10800))) if heavy else 0
    env = os.environ.copy()
    for key in ('AUTO_REPORT_EMAIL_RECEIVER', 'AUTO_REPORT_REQUEST_ID', 'AUTO_REPORT_RUN_ID',
                'AUTO_REPORT_RESULT_PATH', 'AUTO_REPORT_GENERATE_ONLY'):
        env.pop(key, None)
    env.update(AUTO_REPORT_OPS_ROOT=_ops_root(cfg), PYTHONIOENCODING='utf-8',
               AUTO_REPORT_EXECUTION_WAIT_SEC=str(wait_budget))
    proc = subprocess.Popen([cfg['scheduler'].get('python') or sys.executable, 'Main.py', *args],
                            cwd=BASE_DIR, env=env, stdout=output,
                            stderr=subprocess.STDOUT if output else None,
                            start_new_session=(os.name != 'nt'))
    _CURRENT_PROC = proc
    try:return proc.wait(timeout=timeout + wait_budget)
    finally:
        _kill_tree(proc)
        _CURRENT_PROC = None


def _kill_tree(proc):
    """Main.py 는 차트 렌더링 워커(자식 프로세스)를 띄우므로 프로세스 '트리'를 종료한다."""
    global _CURRENT_PROC
    if proc is None or (os.name == 'nt' and proc.poll() is not None):
        return
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        else:
            try:os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:return
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            # The parent can exit before its rendering workers; close the whole group.
            try:os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:pass
        proc.wait(timeout=30)
    except Exception as e:
        log(f"프로세스 종료 실패(pid={proc.pid}): {e}", 'WARN')
        raise RuntimeError('이전 Main 프로세스 트리의 종료를 확인할 수 없어 다음 작업을 시작하지 않습니다') from e


def run_main(cfg, arg, label, email_receiver=None):
    """`python Main.py <arg>` 를 별도 프로세스로 실행하고 종료코드를 반환한다.

    email_receiver 를 주면 환경변수 AUTO_REPORT_EMAIL_RECEIVER 로 전달되어
    My_config.load_from_yaml() 이 config.yaml 의 email_receiver 를 덮어쓴다(트리거 전용 수신처).
    """
    global _CURRENT_PROC
    args = list(arg) if isinstance(arg, (list, tuple)) else [arg]
    arg = ' '.join(args)
    scfg = cfg['scheduler']
    python = (scfg.get('python') or '').strip() or sys.executable
    timeout = int(scfg.get('main_timeout_sec', 10800) or 0)
    wait_budget = max(0, int(scfg.get('execution_lock_wait_sec', 10800)))
    if timeout > 0:timeout += wait_budget

    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    run_id=cfg.get('_run_id') or uuid.uuid4().hex
    env['AUTO_REPORT_RUN_ID']=run_id
    env['AUTO_REPORT_OPS_ROOT']=_ops_root(cfg)
    env['AUTO_REPORT_EXECUTION_WAIT_SEC']=str(wait_budget)
    if cfg.get('_generate_only'):
        env['AUTO_REPORT_GENERATE_ONLY'] = '1'
    else:
        env.pop('AUTO_REPORT_GENERATE_ONLY', None)
    if getattr(sys.stdout, 'isatty', lambda: False)() and not os.getenv('NO_COLOR'):
        env['AUTO_REPORT_COLOR'] = '1'
    result_path=os.path.join(_ops_root(cfg),'results',run_id+'.json')
    env['AUTO_REPORT_RESULT_PATH']=result_path
    if cfg.get('_request_id'):env['AUTO_REPORT_REQUEST_ID']=str(cfg['_request_id'])
    else:env.pop('AUTO_REPORT_REQUEST_ID',None)
    if email_receiver:
        env['AUTO_REPORT_EMAIL_RECEIVER'] = ','.join(email_receiver)
        log(f"수신처 지정: {email_receiver}")
    else:
        env.pop('AUTO_REPORT_EMAIL_RECEIVER', None)

    purpose = ('DB setting 적재(기간·병렬 수 지정, 리포트·메일 없음)' if args[0] == '--init-db' else
               '선택 Lot 수동 처리' if args[0].startswith('_TRIGGER') or args[0] == '--send-user' else
               'DC 데이터 갱신 및 자동 발행')
    log(f"제품 작업 시작 | {label} | {purpose}")
    t0 = time.perf_counter()
    started=time.time()

    try:
        proc = subprocess.Popen(
            [python, 'Main.py', *args],
            cwd=BASE_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, start_new_session=(os.name != 'nt'),
        )
    except Exception as e:
        log(f"Main.py 실행 실패 ({label}): {e}", 'ERROR')
        _atomic_json_file(os.path.join(_ops_root(cfg),'scheduler_runs',run_id+'.json'),
                          dict(id=run_id,vehicle=arg,started=started,finished=time.time(),rc=-1,error=str(e),
                               request_id=cfg.get('_request_id','')))
        return -1

    _CURRENT_PROC = proc
    state = cfg.get('_state')
    if state and state.data.get('active'):
        state.data['active']['child_pid'] = proc.pid
        try:state.save()
        except BaseException:
            _kill_tree(proc);_CURRENT_PROC = None;raise
    heartbeat(cfg,'main',run_id=run_id,vehicle=arg,main_started=time.time())

    def _pump():
        """자식 stdout 을 줄 단위로 스케줄러 로그에 흘려보낸다(블로킹 회피용 스레드)."""
        try:
            stream = io.TextIOWrapper(proc.stdout, encoding='utf-8', errors='replace')
            for line in stream:
                log_child(line.rstrip('\r\n'))
        except Exception:
            pass

    pump = threading.Thread(target=_pump, daemon=True)
    pump.start()

    rc = None
    try:
        while True:
            heartbeat(cfg,'main',run_id=run_id,vehicle=arg)
            try:
                rc=proc.wait(timeout=min(15,max(.1,timeout-(time.perf_counter()-t0))) if timeout>0 else 15)
                break
            except subprocess.TimeoutExpired:
                if timeout>0 and time.perf_counter()-t0>=timeout:raise
                if _STOP.is_set():
                    _kill_tree(proc);rc=-2;break
    except subprocess.TimeoutExpired:
        log(f"타임아웃({timeout}s) → 강제 종료: {label}", 'ERROR')
        _kill_tree(proc)
        rc = -9
    except KeyboardInterrupt:
        _kill_tree(proc)
        rc = -2
        raise
    except BaseException:
        _kill_tree(proc)
        raise
    finally:
        _kill_tree(proc)
        pump.join(timeout=10)
        _CURRENT_PROC = None

    summary=_read_json(result_path,{})
    if summary.get('id')!=run_id and rc not in (-2, -9):
        log(f'Main 결과 누락, 발송 여부 확인 필요: {label}', 'ERROR');rc=-10
    elif rc==0 and summary.get('status')!='success':
        log(f'Main 결과 실패 상태: {label}', 'ERROR');rc=1
    heartbeat(cfg,'main_finished',last_result=rc,last_product_completed=time.time(),vehicle=arg)
    elapsed = time.perf_counter() - t0
    _atomic_json_file(os.path.join(_ops_root(cfg),'scheduler_runs',run_id+'.json'),
                      dict(id=run_id,vehicle=arg,started=started,finished=time.time(),rc=rc,elapsed=elapsed,
                           reports=summary.get('reports',[]),request_id=cfg.get('_request_id',''),
                           error=summary.get('error','')))
    if rc == 0:
        log(f"제품 작업 완료 | {label} | 소요 {elapsed / 60:.1f}분", 'OK')
    else:
        log(f"제품 작업 실패 | {label} | 소요 {elapsed / 60:.1f}분 | 오류 코드 {rc}. 해당 제품 실행 이력을 확인하세요.", 'ERROR')
    return rc


# ==================================================================================================================================
# 트리거 처리 — 대기열을 비울 때까지 1건씩 발행
# ==================================================================================================================================
def process_triggers(cfg, state):
    """pending 의 트리거 요청을 처리한다. 처리한 건수 반환.

    - 요청 1건 = Main.py 1회 실행(_TRIGGER_ 접두어 → 쿼리 없이 현재 DB 로 즉시 발행).
    - 성공/실패와 무관하게 history 에 기록해 **재실행되지 않도록** 한다(max_retry 만큼만 예외).
    """
    if not cfg['trigger'].get('enabled', True):
        return 0

    tcfg = cfg['trigger']
    max_n = int(tcfg.get('max_per_check', 20))
    max_retry = int(tcfg.get('max_retry', 0))
    allowed_kinds = [str(k).strip() for k in (tcfg.get('allowed_kinds', REQUEST_KINDS) or [])]
    allowed = {str(g).strip() for g in (tcfg.get('allowed_email_receiver') or []) if str(g).strip()}
    default_recv = [str(g).strip() for g in (tcfg.get('email_receiver') or []) if str(g).strip()]
    vehicles = known_vehicles()

    handled = 0
    while state.data.get('pending') and handled < max_n and not _STOP.is_set():
        now = time.time()
        index = next((i for i, p in enumerate(state.data['pending'])
                      if p.get('next_attempt_ts', 0) <= now), None)
        if index is None:break
        req = state.data['pending'][index]

        # 제품명 검증(외부 입력) — config.yaml 에 없는 제품이면 즉시 실패 처리
        # (실패 처리 전에 반드시 대기열에서 빼낸다 — 빼지 않으면 같은 건을 계속 다시 집는다)
        if req['vehicle'] not in vehicles:
            state.data['pending'].pop(index)
            req['started_at'] = datetime.now().isoformat(timespec='seconds')
            _finish(state, req, cfg, rc=-3,
                    note=f"config.yaml 에 없는 제품: {req['vehicle']}", ok=False)
            handled += 1
            continue

        # A typo must never silently fall back to a different recipient group.
        recv = req['email_receiver'] if req.get('email_receiver') is not None else default_recv
        if (req.get('kind', 'report') == 'report' and not req.get('generate_only') and
                (not recv or any(g not in allowed for g in recv) or
                 (req.get('mode') in ('FORCE', 'ALL') and len(recv) != 1))):
            state.data['pending'].pop(index)
            _finish(state,req,cfg,rc=-4,note='수신 그룹 누락/허용목록 불일치 또는 FORCE/ALL 수신 그룹이 1개가 아님',ok=False)
            handled += 1
            continue

        state.data['pending'].pop(index)
        req['attempts'] = int(req.get('attempts', 0)) + 1
        req['started_at'] = datetime.now().isoformat(timespec='seconds')
        run_id = uuid.uuid4().hex
        state.data['active'] = dict(request=req, run_id=run_id, child_pid=None,
                                   result_path=os.path.join(_ops_root(cfg), 'results', run_id+'.json'))
        state.save()

        kind = req.get('kind', 'report')
        if kind not in allowed_kinds:
            _finish(state, req, cfg, rc=-5, note=f'허용되지 않은 요청 종류({kind}). scheduler.yaml trigger.allowed_kinds 확인', ok=False)
            handled += 1
            continue
        args = main_arguments(req, recv)
        arg = args[0] if len(args) == 1 else args
        label = {'init_db': '[DB 설치]', 'send_user': '[개인 발송]'}.get(kind, '[TRIGGER]') + f" {target_key(req)}"
        cfg['_request_id']=_dedup_key(req)
        cfg['_generate_only']=req.get('generate_only',False)
        cfg['_run_id']=run_id
        cfg['_state']=state
        # 개인 발송은 Main.py 가 --send-user 로 받는 사람 1명만 쓴다 → 그룹 수신처 환경변수를 넘기지 않는다.
        try:rc = run_main(cfg, arg, label, email_receiver=None if kind in ('init_db', 'send_user') else recv)
        finally:
            cfg.pop('_request_id',None)
            cfg.pop('_generate_only',None)
            cfg.pop('_run_id',None)
            cfg.pop('_state',None)

        # Only failure to launch is certainly safe to retry. Mail may already be sent
        # after a timeout/crash or a completed Main failure; require operator review.
        if rc == -1 and req['attempts'] <= max_retry:
            req['next_attempt_ts'] = time.time() + int(tcfg.get('retry_backoff_sec', 600))
            state.data['pending'].append(req)   # 맨 뒤로 보내 다른 요청을 막지 않게
            state.data['active'] = None
            log(f"트리거 재시도 예약({req['attempts']}/{max_retry}): {target_key(req)}", 'WARN')
            state.save()
        else:
            uncertain = rc in (-2, -9, -10)
            _finish(state, req, cfg, rc=rc, note='결과 미확인; 종료·메일 이력 확인 후 재요청' if uncertain else '',
                    ok=(rc == 0), status='unknown' if uncertain else None)
        handled += 1

    return handled


def recover_active(cfg, state):
    """Resolve an interrupted claim without ever launching the same request again."""
    active = state.data.get('active')
    if not active:return
    pid = active.get('child_pid')
    if pid and _pid_alive(int(pid)):
        raise RuntimeError(f'이전 요청의 Main이 아직 실행 중입니다(pid={pid}); 종료 후 Scheduler를 시작하세요')
    if pid and os.name != 'nt':
        try:os.killpg(int(pid), 0)
        except ProcessLookupError:pass
        except OSError as exc:
            raise RuntimeError(f'이전 Main 프로세스 그룹의 종료를 확인할 수 없습니다(pgid={pid})') from exc
        else:
            raise RuntimeError(f'이전 Main의 자식 프로세스 그룹이 남아 있습니다(pgid={pid}); 확인·정리 후 다시 시작하세요')
    result = _read_json(active.get('result_path', ''), {})
    known = result.get('id') == active.get('run_id') and result.get('status') in ('success', 'failed')
    ok = known and result['status'] == 'success'
    _finish(state, active['request'], cfg, rc=0 if ok else -10,
            note='재시작 시 저장된 Main 결과 확인' if known else '재시작 전 실행 결과 미확인; 자동 재시도 안 함',
            ok=ok, status=None if known else 'unknown')


def _finish(state, req, cfg, rc, note, ok, status=None):
    """처리 결과를 history/done_targets 에 기록하고 inbox 파일을 done/failed 로 옮긴다."""
    dk = _dedup_key(req)
    entry = {
        'req_id': req.get('req_id', ''),
        'mode': req.get('mode','TRIGGER'), 'generate_only': req.get('generate_only',False),
        'kind': req.get('kind', 'report'), 'send_user': req.get('send_user', ''),
        'vehicle': req['vehicle'], 'lot_id': req['lot_id'], 'step_id': req['step_id'],
        'source': req.get('source', ''), 'requested_by': req.get('requested_by', ''),
        'attempts': req.get('attempts', 1),
        'started_at': req.get('started_at', ''),
        'finished_at': datetime.now().isoformat(timespec='seconds'),
        'rc': rc,
        'status': status or ('done' if ok else 'failed'),
        'run_id': (state.data.get('active') or {}).get('run_id', ''),
        'note': note,
    }
    state.data.setdefault('history', {})[dk] = entry
    # 실패한 건도 done_targets 에 남길지: 남기지 않는다(원인 해결 후 재요청 가능해야 하므로).
    # 개인 발송(send_user)은 한 사람에게 보낸 사본이므로 정규 그룹 발행을 막지 않는다.
    if ok and not req.get('generate_only') and req.get('kind', 'report') == 'report':
        for lot, step in _pairs(req['lot_id'], req['step_id']):
            state.data.setdefault('done_targets', {})[f"{req['vehicle']}|{lot}|{step}"] = entry['finished_at']
    state.prune_history(int(cfg['trigger'].get('history_keep', 2000)))
    state.data['active'] = None
    state.save()

    src = req.get('_inbox_file')
    if src and os.path.exists(src):
        _move(src, req['_done_path'] if ok else req['_fail_path'])

    if ok:
        done = ('DB 설치(200일 적재) 완료' if req.get('kind') == 'init_db' else
                '파일 생성·저장 (메일 없음)' if req.get('generate_only') else '메일 상태는 실행 이력에서 확인')
        log(f"수동 요청 처리 완료: {target_key(req)} | {done}", 'OK')
    else:
        log(f"트리거 발행 실패(rc={rc}) {note}: {target_key(req)}", 'ERROR')


# ==================================================================================================================================
# 랏 측정 history 내보내기 (auto report → S3 → flow web)
# ==================================================================================================================================
def export_lot_history(cfg, vehicles=None):
    """RUN/log/{vehicle}_et_log_Final.csv 를 (lot_id, step_id) 단위로 집계해 outbox 에 JSON 으로 저장.

    flow web 은 이 파일을 읽어 '트리거 가능한 제품/랏/스텝 목록'을 사용자에게 보여준다.
    (S3 업로드 자체는 이 스크립트 범위 밖 — outbox 폴더를 그대로 올리면 된다.)
    """
    import csv

    lcfg = cfg.get('lot_history', {})
    if not lcfg.get('enabled', True):
        return []

    out_dir = _abspath(lcfg.get('out_dir', 'RUN/QUEUE/outbox'))
    os.makedirs(out_dir, exist_ok=True)
    max_days = int(lcfg.get('max_days', 30))
    cutoff = datetime.now() - timedelta(days=max_days)

    if vehicles is None:
        vehicles = sorted(all_products(cfg))

    written = []
    for vehicle in vehicles:
        csv_path = os.path.join(BASE_DIR, 'RUN', 'log', f'{vehicle}_et_log_Final.csv')
        if not os.path.exists(csv_path):
            continue
        agg = {}
        try:
            with open(csv_path, 'r', encoding='utf-8', errors='replace', newline='') as f:
                for row in csv.DictReader(f):
                    lot = (row.get('lot_id') or '').strip()
                    step = (row.get('dc_step_id') or '').strip()
                    if not lot or not step:
                        continue
                    tk = (row.get('tkout_time') or '').strip()
                    try:
                        tk_dt = datetime.fromisoformat(tk.replace('/', '-')[:19]) if tk else None
                    except Exception:
                        tk_dt = None
                    if tk_dt and tk_dt < cutoff:
                        continue
                    k = (lot, step)
                    cur = agg.setdefault(k, {
                        'vehicle': vehicle, 'lot_id': lot, 'step_id': step,
                        'key': f'{vehicle}_{lot}_{step}',
                        'wafer_cnt': 0, 'tkout_time': '', 'dc_done': False,
                    })
                    cur['wafer_cnt'] += 1
                    if tk and tk > cur['tkout_time']:
                        cur['tkout_time'] = tk
                    if str(row.get('dc_done', '')).strip().lower() in ('true', '1', 'yes'):
                        cur['dc_done'] = True
        except Exception as e:
            log(f"랏 history 집계 실패({vehicle}): {e}", 'WARN')
            continue

        items = sorted(agg.values(), key=lambda d: d['tkout_time'], reverse=True)
        payload = {
            'schema': 'auto_report.lot_history/1',
            'vehicle': vehicle,
            'generated_at': datetime.now().isoformat(timespec='seconds'),
            'window_days': max_days,
            'count': len(items),
            'items': items,
        }
        out_path = os.path.join(out_dir, f'lot_history_{vehicle}.json')
        tmp = out_path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, out_path)
            written.append(out_path)
        except Exception as e:
            log(f"랏 history 저장 실패({vehicle}): {e}", 'WARN')

    if written:
        log(f"랏 history 내보내기 완료: {len(written)}개 파일 → {out_dir}")
    return written


# ==================================================================================================================================
# 사이클 루프
# ==================================================================================================================================
def _groups(cfg):
    """설정의 그룹 목록을 [(name, every, [products…]), …] 로 정규화(dict/list 양쪽 지원)."""
    raw = cfg['scheduler'].get('groups') or []
    out = []
    if isinstance(raw, dict):
        raw = [dict(g or {}, name=name) for name, g in raw.items()]
    for g in raw:
        if not isinstance(g, dict):
            continue
        name = str(g.get('name') or '?')
        every = max(1, int(g.get('every', 1) or 1))
        prods = [str(p).strip() for p in (g.get('products') or []) if str(p).strip()]
        out.append((name, every, prods))
    return out


def all_products(cfg):
    s = []
    for _n, _e, prods in _groups(cfg):
        for p in prods:
            if p not in s:
                s.append(p)
    return s


def due_groups(cfg, cycle_no):
    """이번 사이클(1부터 시작)에 실행할 그룹 목록."""
    return [(n, e, p) for (n, e, p) in _groups(cfg) if p and cycle_no % e == 0]


def run_cycle(cfg, state):
    """사이클 1회 = due 그룹의 제품을 순서대로 1회씩 실행. 제품 사이마다 트리거를 먼저 비운다."""
    heartbeat(cfg,'cycle',cycle=state.cycle+1)
    cycle_no = state.cycle + 1
    due = due_groups(cfg, cycle_no)
    plan = ' / '.join(f"{n}그룹({e}회 순회마다): {', '.join(p)}" for n, e, p in due) or '(대상 없음)'
    log(f"제품 순회 {cycle_no}회차 시작 | 이번 대상: {plan}")

    gap = int(cfg['scheduler'].get('product_gap_sec', 5))
    for gname, _every, prods in due:
        for vehicle in prods:
            if _STOP.is_set():
                log('정지 요청 감지 → 사이클 중단', 'WARN')
                return
            # 정규 순회보다 트리거가 우선 — 제품 1건 시작 전에 대기열을 비운다
            collect_requests(cfg, state)
            process_triggers(cfg, state)
            if _STOP.is_set():
                return
            run_main(cfg, vehicle, f"[{gname}] {vehicle}")
            export_lot_history(cfg, [vehicle])
            _sleep(gap)

    heartbeat(cfg,'cycle_complete',cycle=cycle_no,last_cycle_completed=time.time())
    state.cycle = cycle_no
    state.save()
    log(f"제품 순회 {cycle_no}회차 종료 | 제품별 성공·실패는 위 결과와 운영 이력에서 확인하세요.", 'OK')


def _sleep(seconds):
    """정지 신호에 즉시 반응하는 sleep."""
    if seconds <= 0:
        return
    _STOP.wait(seconds)


def idle_wait(cfg, state):
    """사이클 사이 유휴 대기 — poll_interval 마다 트리거 큐를 확인하고, 있으면 즉시 처리한다."""
    scfg = cfg['scheduler']
    idle = int(scfg.get('cycle_idle_sec', 300))
    poll = max(5, int(scfg.get('poll_interval_sec', 30)))
    deadline = time.time() + idle
    log(f'다음 제품 순회까지 {idle}초 대기합니다. 대기 중에도 수동 요청을 확인합니다.')
    while time.time() < deadline and not _STOP.is_set():
        heartbeat(cfg,'idle')
        _sleep(min(poll, max(1, deadline - time.time())))
        if _STOP.is_set():
            return
        if collect_requests(cfg, state) or state.data.get('pending'):
            process_triggers(cfg, state)


# ==================================================================================================================================
# 단일 인스턴스 잠금 / 상태 파일
# ==================================================================================================================================
def _pid_alive(pid):
    try:
        if os.name == 'nt':
            out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'],
                                 capture_output=True, text=True, timeout=20).stdout
            # 부분일치 오탐 방지: PID 열에서 정확히 일치하는 숫자만 인정
            return re.search(rf'(?m)^\S+\s+{int(pid)}\b', out) is not None
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def acquire_lock(lock_path, force=False):
    """스케줄러 중복 기동 방지. 잠금 획득 시 True."""
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    guard=open(lock_path+'.guard','a+b')
    try:
        guard.seek(0,2)
        if guard.tell()==0:guard.write(b'0');guard.flush()
        guard.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(guard.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(guard.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        guard.close();log('동일 프로세스 역할의 OS 잠금이 사용 중입니다.','ERROR');return False
    if os.path.exists(lock_path):
        try:
            with open(lock_path, 'r', encoding='utf-8') as f:
                old = json.load(f)
            pid = int(old.get('pid', 0))
        except Exception:
            pid = 0
        if pid and _pid_alive(pid) and not force:
            guard.close()
            log(f"잠금 기록의 PID가 실행 중입니다 (pid={pid}). 실제 실행 경로를 확인하세요.", 'ERROR')
            return False
    _atomic_json_file(lock_path, {'pid': os.getpid(),
                                 'started_at': datetime.now().isoformat(timespec='seconds')})
    _LOCK_FILES.append(guard)
    return True


def write_status(cfg, state, phase, status_path):
    """모니터링/웹 조회용 현재 상태 스냅샷."""
    try:
        payload = {
            'pid': os.getpid(),
            'updated_at': datetime.now().isoformat(timespec='seconds'),
            'phase': phase,
            'cycle': state.cycle,
            'next_cycle_groups': [n for n, _e, _p in due_groups(cfg, state.cycle + 1)],
            'pending': len(state.data.get('pending', [])),
            'active': state.data.get('active'),
            'history': len(state.data.get('history', {})),
        }
        tmp = status_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, status_path)
    except Exception:
        pass


# ==================================================================================================================================
# CLI
# ==================================================================================================================================
def _on_signal(signum, _frame):
    log(f'정지 신호 수신({signum}) → 현재 작업을 정리하고 종료합니다.', 'WARN')
    _STOP.set()
    _kill_tree(_CURRENT_PROC)


def cmd_enqueue(cfg, key_or_json):
    """테스트/수동 투입: inbox 에 요청 파일 1건 생성(.tmp → rename 규칙 준수)."""
    try:
        raw = json.loads(key_or_json)
    except Exception:
        raw = {'key': key_or_json}
    if not isinstance(raw, dict):
        log('요청은 JSON 객체 또는 제품_Lot_Step이어야 합니다', 'ERROR');return 2
    if not raw.get('req_id'):raw['req_id'] = 'manual-' + uuid.uuid4().hex
    raw.setdefault('requested_by', 'cli')
    raw.setdefault('requested_at', datetime.now().isoformat(timespec='seconds'))

    req, why = _norm_request(raw, 'cli')
    if req is None:
        log(f'요청 형식 오류: {why}', 'ERROR')
        return 2
    tcfg = cfg['trigger']
    if req['vehicle'] not in known_vehicles():
        log('config.yaml에서 확인할 수 없는 제품입니다: ' + req['vehicle'], 'ERROR');return 2
    if req['kind'] not in (tcfg.get('allowed_kinds', REQUEST_KINDS) or []):
        log('허용되지 않은 요청 종류입니다: ' + req['kind'], 'ERROR');return 2
    recv = req['email_receiver'] if req.get('email_receiver') is not None else tcfg.get('email_receiver', [])
    allowed = tcfg.get('allowed_email_receiver') or []
    if (req['kind'] == 'report' and not req['generate_only'] and
            (not recv or any(g not in allowed for g in recv) or
             (req['mode'] in ('FORCE', 'ALL') and len(recv) != 1))):
        log('수신 그룹 누락/허용목록 불일치 또는 FORCE/ALL 수신 그룹이 1개가 아님', 'ERROR');return 2

    inbox = _abspath(cfg['trigger'].get('inbox_dir', 'RUN/QUEUE/inbox'))
    os.makedirs(inbox, exist_ok=True)
    name = f"{req['req_id']}.json"
    path = os.path.join(inbox, name)
    tmp = path + '.' + uuid.uuid4().hex + '.tmp'
    try:
        _atomic_json_file(tmp, raw)
        os.link(tmp, path)  # Atomic publication that cannot overwrite another producer.
    except FileExistsError:
        log(f'같은 req_id의 inbox 파일이 이미 있습니다: {path}', 'ERROR');return 2
    finally:
        if os.path.exists(tmp):os.remove(tmp)
    log(f"트리거 투입 완료: {target_key(req)} → {os.path.join(inbox, name)}")
    return 0


def cmd_status(cfg, state):
    print(f"사이클: {state.cycle} (다음 #{state.cycle + 1} 실행 그룹: "
          f"{[n for n, _e, _p in due_groups(cfg, state.cycle + 1)] or '없음'})")
    print(f"대기 트리거: {len(state.data.get('pending', []))}건")
    active = state.data.get('active') or {}
    if active:
        print(f"실행 요청: {target_key(active['request'])} (req_id={active['request'].get('req_id')}, pid={active.get('child_pid')})")
    for p in state.data.get('pending', []):
        print(f"  - {target_key(p)} (req_id={p.get('req_id') or '-'}, attempts={p.get('attempts', 0)})")
    hist = state.data.get('history', {})
    print(f"처리 이력: {len(hist)}건 (최근 10건)")
    recent = sorted(hist.items(), key=lambda kv: kv[1].get('finished_at', ''), reverse=True)[:10]
    for k, v in recent:
        print(f"  - [{v.get('status')}] {v.get('vehicle')}|{v.get('lot_id')}|{v.get('step_id')} "
              f"@{v.get('finished_at')} rc={v.get('rc')}")
    return 0


def cmd_request_status(cfg, state, req_id):
    if not _RE_REQUEST_ID.fullmatch(req_id):
        log('req_id 형식 오류', 'ERROR');return 2
    entry = state.data.get('history', {}).get('id:' + req_id)
    if entry is None:
        active = state.data.get('active') or {}
        if active.get('request', {}).get('req_id') == req_id:
            entry = dict(active['request'], status='running', run_id=active.get('run_id'), child_pid=active.get('child_pid'))
        else:
            entry = next((dict(p, status='pending') for p in state.data.get('pending', []) if p.get('req_id') == req_id), None)
    if entry is None:
        path = os.path.join(_abspath(cfg['trigger'].get('inbox_dir', 'RUN/QUEUE/inbox')), req_id+'.json')
        entry = dict(req_id=req_id, status='inbox' if os.path.isfile(path) else 'not_found')
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    return 0


def _scheduler_cli():
    global _LOG_PATH, _ACTIVE_CONFIG

    ap = argparse.ArgumentParser(description='Auto Report 제품 순회 스케줄러 + 트리거 큐 소비기')
    actions = ap.add_mutually_exclusive_group()
    actions.add_argument('--watchdog', action='store_true', help='독립 Watchdog 상시 실행')
    actions.add_argument('--watchdog-once', action='store_true', help='Watchdog 1회 점검')
    actions.add_argument('--watchdog-preview', action='store_true', help='메일 없이 Watchdog HTML/CSV 미리보기')
    actions.add_argument('--daily-trend', action='store_true', help='선택 제품 Daily Trend 독립 타이머')
    actions.add_argument('--daily-trend-once', action='store_true', help='Daily Trend 1회 실행')
    actions.add_argument('--daily-trend-preview', action='store_true', help='메일 없이 Daily Trend HTML/PPT 미리보기')
    actions.add_argument('--mlmode', action='store_true', help='power admin용 ML 이상 Trend 독립 타이머')
    actions.add_argument('--mlmode-once', action='store_true', help='ML mode 1회 실행')
    actions.add_argument('--mlmode-preview', action='store_true', help='메일 없이 ML mode 미리보기')
    actions.add_argument('--once', action='store_true', help='사이클 1회만 수행하고 종료')
    actions.add_argument('--drain', action='store_true', help='대기 중인 트리거만 처리하고 종료')
    actions.add_argument('--status', action='store_true', help='현재 상태 요약 출력')
    actions.add_argument('--request-status', metavar='REQ_ID', help='요청 1건의 접수/실행/결과 JSON 읽기(쓰기 없음)')
    actions.add_argument('--enqueue', metavar='KEY|JSON', help='트리거 수동 투입 (제품_lotid_stepid 또는 JSON)')
    actions.add_argument('--export-history', action='store_true', help='랏 history outbox 재생성만 수행')
    ap.add_argument('--config', default=CONFIG_PATH, help='scheduler.yaml 경로')
    ap.add_argument('--force', action='store_true', help='OS 잠금 획득 후 남은 PID 기록만 무시(실행 중 잠금 우회 불가)')
    args = ap.parse_args()

    _LOG_PATH = None if (args.status or args.request_status) else os.path.join(BASE_DIR, 'RUN', 'log', 'scheduler_log.txt')
    if not (args.status or args.request_status or args.enqueue):
        from runtime_versions import snapshot
        version = snapshot(BASE_DIR)
        log(f"현재 코드 버전: {version['id']} / {version.get('created_at', '')} / {version.get('label', '')}", 'OK')
    cfg = load_config(args.config,create_if_missing=not (args.status or args.request_status),strict_services=False)
    _ACTIVE_CONFIG=cfg
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:signal.signal(sig, _on_signal)
        except Exception:pass
    if args.watchdog or args.watchdog_once or args.watchdog_preview:
        return run_watchdog(cfg,args.config,once=args.watchdog_once,preview=args.watchdog_preview)
    if args.daily_trend or args.daily_trend_once or args.daily_trend_preview:
        return run_daily_trend(cfg,args.config,once=args.daily_trend_once,preview=args.daily_trend_preview)
    if args.mlmode or args.mlmode_once or args.mlmode_preview:
        return run_daily_trend(cfg,args.config,once=args.mlmode_once,preview=args.mlmode_preview,service='mlmode')

    queue_root = _abspath(cfg['trigger'].get('queue_root', 'RUN/QUEUE'))
    state = State(os.path.join(queue_root, 'scheduler_state.json'))
    status_path = os.path.join(queue_root, 'scheduler_status.json')

    if args.status:
        return cmd_status(cfg, state)
    if args.request_status:
        return cmd_request_status(cfg, state, args.request_status)
    if args.enqueue:
        return cmd_enqueue(cfg, args.enqueue)
    if args.export_history:
        export_lot_history(cfg)
        return 0

    if not acquire_lock(os.path.join(queue_root, 'scheduler.lock'), force=args.force):
        return 1
    # Load only after owning the consumer lock, so a former consumer's last save wins.
    state = State(state.path)
    recover_active(cfg, state)

    heartbeat(cfg,'starting')
    if not args.once and not args.drain:
        start_background_services(cfg,args.config)
    log('자동 운영 시작 | 제품 순서대로 DC 데이터를 갱신하고 발행 대상을 확인합니다.', 'OK')
    log('운영 요청은 CLI 또는 수동 큐로 접수합니다. Ctrl+C는 제품 순회를 중단합니다.')
    for n, e, p in _groups(cfg):
        log(f"  {n}그룹: {e}회 순회마다 처리 · 제품 순서: {' → '.join(p) or '(없음)'}")

    try:
        if args.drain:
            write_status(cfg, state, 'drain', status_path)
            collect_requests(cfg, state)
            n = process_triggers(cfg, state)
            log(f'트리거 처리 {n}건 완료')
            return 0

        while not _STOP.is_set():
            write_status(cfg, state, 'cycle', status_path)
            collect_requests(cfg, state)
            process_triggers(cfg, state)
            if _STOP.is_set():
                break
            run_cycle(cfg, state)
            if not args.once:start_background_services(cfg,args.config)
            export_lot_history(cfg)
            if args.once or _STOP.is_set():
                break
            write_status(cfg, state, 'idle', status_path)
            idle_wait(cfg, state)
    except KeyboardInterrupt:
        log('사용자 중단(Ctrl+C)', 'WARN')
        _STOP.set()
    finally:
        _kill_tree(_CURRENT_PROC)
        heartbeat(cfg,'stopped')
        write_status(cfg, state, 'stopped', status_path)
        state.save()
        log('스케줄러 종료')
    return 0


def main():
    # Read-only status/help and inbox submission do not start a runtime lease or create history.
    if any(arg in sys.argv[1:] for arg in ('--status', '--request-status', '--enqueue', '--help', '-h')):
        return _scheduler_cli()
    from runtime_versions import runtime_lease
    with runtime_lease(BASE_DIR):
        return _scheduler_cli()


if __name__ == '__main__':
    sys.exit(main() or 0)
