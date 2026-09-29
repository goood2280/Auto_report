"""Lightweight operator messages; no report, network or model imports."""
import os
import re
import sys

STAGES = {
    'startup': '작업 준비', 'waiting_execution': '앞선 작업 종료 대기', 'configuration': '제품 설정 확인',
    'et_query': 'DC 측정 데이터 조회·DB 적재', 'wip_query': 'Lot 공정 진행 현황 조회',
    'measurement_selection': '측정 완료 Lot·DC Step 및 발행 대상 확인',
    'raw_load': '분석할 측정 데이터 읽기', 'pivot_addp': '측정 항목 정리·파생값 계산',
    'coordinate_join': 'Wafer 측정 좌표 연결', 'report_prepare': '선택 Lot 리포트 준비',
    'inline_query': 'Root Lot의 Inline 측정 결과 조회', 'chart_render': '추세·Wafer Map 차트 작성',
    'analysis': 'Spec 이탈·산포·변화 통계 분석', 'ppt_save': 'PPT 리포트 저장',
    'html_save': '메일 본문용 HTML 저장', 'score_save': 'Pass Rate 결과 저장',
    's3_upload': '공유 저장소 업로드 확인', 'email': '리포트 메일 발송',
    'email_retry_saved': '저장된 리포트로 메일 발송 재시도', 'finished': '작업 종료',
    'between_reports': '개별 리포트 결과 기록',
}
ANSI = re.compile(r'\x1b\[[0-9;]*m')

def plain(text):
    return ANSI.sub('', str(text))

def color(text, state='info'):
    if os.getenv('NO_COLOR') or not (os.getenv('AUTO_REPORT_COLOR') == '1' or getattr(sys.stdout, 'isatty', lambda: False)()):
        return str(text)
    if os.name == 'nt':
        try:
            import ctypes
            kernel = ctypes.windll.kernel32
            kernel.SetConsoleMode(kernel.GetStdHandle(-11), 7)
        except Exception:
            pass
    code = {'ok':92, 'info':96, 'warn':93, 'error':91}.get(state,96)
    return f'\x1b[{code}m{text}\x1b[0m'
