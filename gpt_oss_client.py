# -*- coding: utf-8 -*-
"""
GPT OSS 120B 연결 우회(Mock) 모듈
수집된 metrics_dict와 GPT_MANUAL.md의 룰을 바탕으로
상위 6개 차트 선정 및 이상 요약 HTML을 반환합니다.
"""

import os
import base64

def generate_report_summary(metrics_dict):
    """
    Args:
        metrics_dict (dict): { item_name: { global_med, target_med, deviation, spec_outs, ... }, ... }
    
    Returns:
        tuple: (gpt_summary_html, top_6_items)
    """
    print("[MOCK-GPT] GPT OSS 120B 더미 호출 및 데이터 해석 중...")
    
    # 1. 이상 순위 선정 (Ranking Logic)
    # 1순위: spec_out_count 내림차순, 2순위: deviation 내림차순
    sorted_items = sorted(
        metrics_dict.values(),
        key=lambda x: (x.get('spec_out_count', 0), x.get('deviation', 0)),
        reverse=True
    )
    
    # 이상이 있는(편차가 0보다 크거나 spec_out이 있는) 상위 6개만 필터
    top_items = [x for x in sorted_items if x.get('spec_out_count', 0) > 0 or x.get('deviation', 0) > 1.5][:6]
    top_item_names = [x['item'] for x in top_items]
    
    if not top_items:
        return """
        <ul style="font-size: 14px; color: #333; margin-top: 5px; margin-bottom: 15px; padding-left: 20px;">
            <li style="margin-bottom: 6px;"><strong>[요약]</strong>: 전체 계측 항목이 정상 관리 규격 내에 있으며 통계적으로 유의미한 산포 변동이 없습니다.</li>
            <li><strong>[권고 조치]</strong>: 특이사항 없으므로 정상 진행 바랍니다.</li>
        </ul>
        """, []

    # 2. 불량 모드 추론 (GPT_MANUAL.md 룰 적용)
    # 실제로는 GPT가 해석하겠지만, Mock에서는 Rule을 하드코딩 또는 간단 매칭으로 구현
    matched_rule = None
    comment = "통계적 산포 변동이 감지되었습니다. 원인 분석이 필요합니다."
    link = "#"
    
    # Rule 1
    if "ET_RCNT_N" in top_item_names or "ET_RCNT_P" in top_item_names:
        matched_rule = "AAA 모드"
        comment = "Contact 저항 스펙 초과 발생. 식각 불량(미오픈) 혹은 폴리머 잔류 의심."
        link = "http://dashboard.internal/contact_defect"
    # Rule 2-1
    elif "ET_VTH_N" in top_item_names and "ET_VTH_P" in top_item_names:
        matched_rule = "BBB 모드"
        comment = "N/P VTH가 동시에 틀어지는 현상. Gate CD 타겟 미달 혹은 Gate Oxide 두께 산포 변동 의심."
        link = "http://dashboard.internal/gate_module"
    # Rule 2-2
    elif "ET_VTH_N" in top_item_names:
        matched_rule = "CCC 모드"
        comment = "N-MOS 임계전압 단독 변동. N-Well 이온주입 공정 산포 이상 혹은 채널 도핑 문제 의심."
        link = "http://dashboard.internal/nwell_module"

    # 3. Spec-out 핫스팟 추출
    hotspots = []
    for item in top_items:
        if item.get('spec_outs'):
            pts = item['spec_outs']
            if len(pts) > 0:
                hotspots.append(f"{item['item']} (X:{pts[0].get('x','?')}, Y:{pts[0].get('y','?')})")
    
    hotspots_str = ", ".join(hotspots) if hotspots else "특이 핫스팟 없음"
    items_str = ", ".join([f"<b>{x['item']}</b> (Dev: {x['deviation']}σ)" for x in top_items])
    
    html = f"""
    <ul style="font-size: 14px; color: #333; margin-top: 5px; margin-bottom: 15px; padding-left: 20px;">
        <li style="margin-bottom: 6px;"><strong>[Top {len(top_items)} 요약]</strong>: {items_str} 항목에서 이상 변동이 감지되었습니다.</li>
        <li style="margin-bottom: 6px;"><strong>[불량 모드 추정]</strong>: {matched_rule if matched_rule else '알 수 없는 모드'}</li>
        <li style="margin-bottom: 6px;"><strong>[현상 분석 및 추정 원인]</strong>: {comment}</li>
        <li style="margin-bottom: 6px;"><strong>[참고 링크]</strong>: <a href="{link}" target="_blank">관련 대시보드 확인</a></li>
        <li><strong>[Spec-out 핫스팟]</strong>: {hotspots_str}</li>
    </ul>
    """
    
    return html, top_item_names
