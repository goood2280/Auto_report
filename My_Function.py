import ast
import operator
import os
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt


DEFAULT_HTML_LIMIT_BYTES = 2 * 1024 * 1024
DEFAULT_PPT_LIMIT_BYTES = 10 * 1024 * 1024
TOKEN_RE = re.compile(r"\{([^{}]+)\}")


def bytes_from_mb(value, default):
    if value is None:
        return default
    return int(float(value) * 1024 * 1024)


def addp_tokens(form):
    if pd.isna(form):
        return []
    return [token.strip() for token in TOKEN_RE.findall(str(form)) if token.strip()]


def required_real_items(df_reformatter):
    items = set()
    for _, row in df_reformatter.iterrows():
        form = row.get("ADDP Form", row.get("ADDP_FORM", ""))
        tokens = addp_tokens(form)
        if tokens:
            items.update(tokens)
            continue
        real_item = row.get("REAL ITEM", row.get("REAL_ITEM", ""))
        if isinstance(real_item, str) and real_item.strip():
            items.add(real_item.strip())
        elif isinstance(row.get("ALIAS"), str):
            items.add(row["ALIAS"].strip())
    return items


def reformatter_verify(df_reformatter):
    required = {"ALIAS", "REPORT ORDER"}
    missing = sorted(required - set(df_reformatter.columns))
    if missing:
        print(f"[ERROR] reformatter missing columns: {missing}")
        return False
    if "ADDP Form" not in df_reformatter.columns:
        print("[WARN] reformatter has no 'ADDP Form' column; ALIAS columns will be used directly when present")
    return True


def normalize_coordinate_columns(df):
    rename_map = {
        "mask": "MASK",
        "chip_x_pos": "CHIP_X_POS",
        "chip_y_pos": "CHIP_Y_POS",
        "flat_zone": "FLAT_ZONE_POS",
    }
    out = df.copy()
    for src, dst in rename_map.items():
        if dst not in out.columns and src in out.columns:
            out[dst] = out[src]
    return out


def materialize_wide_columnbase(df_columnbase, needed_items, config):
    item_col = config.get("item_id_column", "item_id")
    value_col = config.get("value_column", "et_value")
    key_cols = [col for col in config.get("identity_columns", []) if col in df_columnbase.columns]

    if item_col not in df_columnbase.columns or value_col not in df_columnbase.columns:
        return df_columnbase.copy()

    filtered = df_columnbase[df_columnbase[item_col].astype(str).isin(set(needed_items))].copy()
    if filtered.empty:
        raise ValueError("Columnbase data did not contain any real items required by the reformatter")

    wide = (
        filtered.pivot_table(
            index=key_cols,
            columns=item_col,
            values=value_col,
            aggfunc="first",
        )
        .reset_index()
        .rename_axis(None, axis=1)
    )
    return wide


def evaluate_addp_formula(df, formula):
    formula = str(formula)
    tokens = addp_tokens(formula)
    if not tokens:
        raise ValueError("ADDP formula has no {REAL_ITEM} tokens")

    names = {}
    expression = formula
    for index, token in enumerate(tokens):
        name = f"v{index}"
        if token not in df.columns:
            raise KeyError(f"Required real item column is missing: {token}")
        names[name] = pd.to_numeric(df[token], errors="coerce")
        expression = expression.replace("{" + token + "}", name)

    tree = ast.parse(expression, mode="eval")
    return _eval_ast(tree.body, names)


def _eval_ast(node, names):
    bin_ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
    }
    unary_ops = {ast.UAdd: operator.pos, ast.USub: operator.neg}

    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name) and node.id in names:
        return names[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in bin_ops:
        return bin_ops[type(node.op)](_eval_ast(node.left, names), _eval_ast(node.right, names))
    if isinstance(node, ast.UnaryOp) and type(node.op) in unary_ops:
        return unary_ops[type(node.op)](_eval_ast(node.operand, names))
    raise ValueError(f"Unsupported ADDP expression: {ast.dump(node)}")


def apply_reformatter(df_wide, df_reformatter):
    out = df_wide.copy()
    for _, row in df_reformatter.sort_values("REPORT ORDER").iterrows():
        alias = str(row["ALIAS"]).strip()
        form = row.get("ADDP Form", row.get("ADDP_FORM", ""))
        tokens = addp_tokens(form)
        if tokens:
            out[alias] = evaluate_addp_formula(out, form)
            continue

        real_item = row.get("REAL ITEM", row.get("REAL_ITEM", ""))
        source = str(real_item).strip() if isinstance(real_item, str) and real_item.strip() else alias
        if source in out.columns and alias not in out.columns:
            out[alias] = out[source]
    return out


def merge_coordinate_file(df, config):
    coord_path = config.get("coordinate_file_path")
    if not coord_path or not Path(coord_path).exists():
        return df

    coord = pd.read_excel(coord_path, sheet_name=None)
    if "Zone_Define" not in coord:
        return df

    zone = coord["Zone_Define"]
    keys = ["MASK", "CHIP_X_POS", "CHIP_Y_POS", "FLAT_ZONE_POS"]
    if all(col in df.columns for col in keys) and all(col in zone.columns for col in keys):
        return pd.merge(df, zone, on=keys, how="left")
    return df


def prepare_report_dataframe(df_columnbase, df_reformatter, config):
    needed_items = required_real_items(df_reformatter)
    wide = materialize_wide_columnbase(df_columnbase, needed_items, config)
    report = apply_reformatter(wide, df_reformatter)
    report = normalize_coordinate_columns(report)

    if "wafer_id" in report.columns:
        report["wafer_id"] = pd.to_numeric(report["wafer_id"], errors="coerce").astype("Int64")
    if "tkout_time" in report.columns:
        report["tkout_time"] = pd.to_datetime(report["tkout_time"], errors="coerce")
    if "fab_lot_id" not in report.columns and "lot_id" in report.columns:
        report["fab_lot_id"] = report["lot_id"]
    if "root_lot_id" not in report.columns and "fab_lot_id" in report.columns:
        report["root_lot_id"] = report["fab_lot_id"].astype(str).str[:5]

    report = merge_coordinate_file(report, config)
    return report


def _rgb(hex_color):
    hex_color = hex_color.strip("#")
    return RGBColor(int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))


def _add_textbox(slide, left, top, width, height, text, font_size=18, bold=False, color="1F2937"):
    box = slide.shapes.add_textbox(left, top, width, height)
    frame = box.text_frame
    frame.clear()
    frame.word_wrap = True
    p = frame.paragraphs[0]
    p.text = str(text)
    p.font.name = "Arial"
    p.font.size = Pt(font_size)
    p.font.bold = bold
    p.font.color.rgb = _rgb(color)
    return box


def _add_title(slide, title, subtitle=None):
    _add_textbox(slide, Inches(0.55), Inches(0.32), Inches(11.8), Inches(0.45), title, 24, True, "0B1F33")
    line = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.55), Inches(0.86), Inches(1.25), Inches(0.05))
    line.fill.solid()
    line.fill.fore_color.rgb = _rgb("00A3A3")
    line.line.fill.background()
    if subtitle:
        _add_textbox(slide, Inches(1.95), Inches(0.77), Inches(10.4), Inches(0.25), subtitle, 10, False, "6B7280")


def _add_footer(slide, text):
    _add_textbox(slide, Inches(0.45), Inches(7.05), Inches(12.4), Inches(0.25), text, 8, False, "6B7280")


def _safe_number(value, default=np.nan):
    try:
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def build_item_summary(df, df_reformatter):
    rows = []
    for _, spec in df_reformatter.dropna(subset=["ALIAS"]).iterrows():
        alias = str(spec["ALIAS"]).strip()
        if alias not in df.columns:
            continue
        values = pd.to_numeric(df[alias], errors="coerce").dropna()
        if values.empty:
            continue

        low = _safe_number(spec.get("SPECLOW"))
        high = _safe_number(spec.get("SPECHIGH"))
        pass_mask = pd.Series(True, index=values.index)
        if not np.isnan(low):
            pass_mask &= values >= low
        if not np.isnan(high):
            pass_mask &= values <= high

        rows.append(
            {
                "Item": alias,
                "Count": int(values.count()),
                "Mean": float(values.mean()),
                "Min": float(values.min()),
                "Max": float(values.max()),
                "Spec Low": "" if np.isnan(low) else low,
                "Spec High": "" if np.isnan(high) else high,
                "Pass Rate": float(pass_mask.mean() * 100),
            }
        )
    return pd.DataFrame(rows)


def _format_cell(value):
    if isinstance(value, float):
        return f"{value:.3g}"
    return "" if pd.isna(value) else str(value)


def _add_table(slide, df, left, top, width, height, font_size=9, max_rows=10):
    display = df.head(max_rows).copy()
    rows = len(display) + 1
    cols = max(1, len(display.columns))
    table = slide.shapes.add_table(rows, cols, left, top, width, height).table
    for col_idx, col in enumerate(display.columns):
        cell = table.cell(0, col_idx)
        cell.text = str(col)
        cell.fill.solid()
        cell.fill.fore_color.rgb = _rgb("0B1F33")
        for para in cell.text_frame.paragraphs:
            para.font.name = "Arial"
            para.font.size = Pt(font_size)
            para.font.bold = True
            para.font.color.rgb = RGBColor(255, 255, 255)
            para.alignment = PP_ALIGN.CENTER

    for row_idx, (_, row) in enumerate(display.iterrows(), start=1):
        for col_idx, col in enumerate(display.columns):
            cell = table.cell(row_idx, col_idx)
            cell.text = _format_cell(row[col])
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb("F8FAFC") if row_idx % 2 else _rgb("FFFFFF")
            for para in cell.text_frame.paragraphs:
                para.font.name = "Arial"
                para.font.size = Pt(font_size)
                para.font.color.rgb = _rgb("1F2937")
                para.alignment = PP_ALIGN.CENTER
    return table


def _score_board_tables(item_summary, detail_df):
    items = [item for item in item_summary.get("Item", pd.Series(dtype=str)).tolist() if item in detail_df.columns]
    if not items or "wafer_id" not in detail_df.columns:
        fallback = item_summary[["Item", "Pass Rate"]].copy() if "Pass Rate" in item_summary else item_summary.copy()
        return fallback, fallback

    wafers = sorted(pd.Series(detail_df["wafer_id"]).dropna().unique().tolist())[:12]
    pass_rows = []
    value_rows = []

    for item in items:
        spec_row = item_summary[item_summary["Item"] == item].iloc[0]
        low = _safe_number(spec_row.get("Spec Low"))
        high = _safe_number(spec_row.get("Spec High"))
        pass_row = {"Index": item}
        value_row = {"Index": item}

        for wafer in wafers:
            values = pd.to_numeric(detail_df.loc[detail_df["wafer_id"] == wafer, item], errors="coerce").dropna()
            label = f"W{int(wafer):02d}" if float(wafer).is_integer() else f"W{wafer}"
            if values.empty:
                pass_row[label] = ""
                value_row[label] = ""
                continue
            pass_mask = pd.Series(True, index=values.index)
            if not np.isnan(low):
                pass_mask &= values >= low
            if not np.isnan(high):
                pass_mask &= values <= high
            pass_row[label] = round(float(pass_mask.mean() * 100), 1)
            value_row[label] = round(float(values.median()), 3)
        pass_rows.append(pass_row)
        value_rows.append(value_row)

    return pd.DataFrame(pass_rows), pd.DataFrame(value_rows)


def _add_score_board_table(slide, df, left, top, width, height, font_size=9):
    table = _add_table(slide, df, left, top, width, height, font_size=font_size, max_rows=20)
    for r in range(1, len(df) + 1):
        for c in range(1, len(df.columns)):
            cell = table.cell(r, c)
            try:
                value = float(cell.text)
            except Exception:
                continue
            cell.fill.solid()
            if value >= 95:
                cell.fill.fore_color.rgb = _rgb("DCFCE7")
            elif value >= 80:
                cell.fill.fore_color.rgb = _rgb("FEF9C3")
            else:
                cell.fill.fore_color.rgb = _rgb("FEE2E2")
    return table


def _add_score_board_slide(prs, title, subtitle, board_df, note):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_title(slide, title, subtitle)
    _add_score_board_table(slide, board_df, Inches(0.55), Inches(1.25), Inches(12.25), Inches(4.9), 8)

    legend_items = [(">=95", "DCFCE7"), ("80-94.9", "FEF9C3"), ("<80", "FEE2E2")]
    for i, (label, color) in enumerate(legend_items):
        left = Inches(0.65 + i * 1.25)
        box = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, Inches(6.35), Inches(0.25), Inches(0.18))
        box.fill.solid()
        box.fill.fore_color.rgb = _rgb(color)
        box.line.color.rgb = _rgb("CBD5E1")
        _add_textbox(slide, left + Inches(0.32), Inches(6.28), Inches(0.85), Inches(0.25), label, 8, False, "6B7280")
    _add_footer(slide, note)
    return slide


def build_mail_html(target_lot, target_step_id, item_summary, detail_df, max_bytes=DEFAULT_HTML_LIMIT_BYTES):
    summary_html = item_summary.to_html(index=False, border=0, classes="summary")
    item_cols = [col for col in item_summary.get("Item", pd.Series(dtype=str)).tolist() if col in detail_df.columns]
    detail_cols = [
        col
        for col in ["fab_lot_id", "wafer_id", "step_id", "tkout_time", "eqp_id", "CHIP_X_POS", "CHIP_Y_POS"] + item_cols
        if col in detail_df.columns
    ]
    if not detail_cols:
        detail_cols = list(detail_df.columns[:8])

    style = """
<style>
body{font-family:Arial,sans-serif;color:#1f2937;margin:0;padding:16px;background:#f8fafc}
.wrap{max-width:1180px;margin:0 auto;background:#fff;border:1px solid #e5e7eb}
.head{background:#0b1f33;color:#fff;padding:18px 22px}
.head h1{font-size:22px;margin:0 0 4px}.head p{font-size:12px;margin:0;color:#cbd5e1}
.section{padding:16px 22px}.section h2{font-size:16px;margin:0 0 10px;color:#0b1f33}
table{border-collapse:collapse;width:100%;font-size:12px}th{background:#0b1f33;color:#fff}
th,td{border:1px solid #e5e7eb;padding:5px 7px;text-align:center}tr:nth-child(even){background:#f8fafc}
.note{font-size:11px;color:#6b7280;padding:12px 22px 18px}
</style>
"""
    rows = min(len(detail_df), 200)
    while rows >= 5:
        detail_html = detail_df[detail_cols].head(rows).to_html(index=False, border=0, classes="detail")
        html = f"""<!doctype html><html><head><meta charset="utf-8">{style}</head><body>
<div class="wrap">
<div class="head"><h1>AUTO REPORT - {target_lot}</h1><p>Step {target_step_id} | generated {datetime.now():%Y-%m-%d %H:%M}</p></div>
<div class="section"><h2>Summary</h2>{summary_html}</div>
<div class="section"><h2>Data Sample</h2>{detail_html}</div>
<div class="note">This is an automated mail summary. The attached PPT contains the report package.</div>
</div></body></html>"""
        if len(html.encode("utf-8")) <= max_bytes:
            return html
        rows //= 2
    raise ValueError("HTML report exceeds size limit after row reduction")


def build_professional_ppt(target_lot, target_root, target_step_id, vehicle, prod, item_summary, detail_df):
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = _rgb("0B1F33")
    accent = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(6.85), Inches(13.333), Inches(0.65))
    accent.fill.solid()
    accent.fill.fore_color.rgb = _rgb("00A3A3")
    accent.line.fill.background()
    _add_textbox(slide, Inches(0.75), Inches(1.35), Inches(10.8), Inches(0.8), f"{vehicle} Auto Report", 34, True, "FFFFFF")
    _add_textbox(slide, Inches(0.78), Inches(2.25), Inches(10.8), Inches(0.4), f"{target_lot} | {target_step_id} | {prod}", 18, False, "CBD5E1")
    _add_textbox(slide, Inches(0.78), Inches(5.95), Inches(10), Inches(0.3), datetime.now().strftime("%Y-%m-%d %H:%M"), 10, False, "E5E7EB")

    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_title(slide, "Executive Summary", f"Lot {target_lot} / Root {target_root} / Step {target_step_id}")
    total_sites = int(len(detail_df))
    wafers = int(detail_df["wafer_id"].nunique()) if "wafer_id" in detail_df.columns else 0
    avg_pass = item_summary["Pass Rate"].mean() if "Pass Rate" in item_summary else np.nan
    cards = [("Measured Sites", f"{total_sites:,}"), ("Wafers", str(wafers)), ("Avg Pass Rate", f"{avg_pass:.1f}%")]
    for i, (label, value) in enumerate(cards):
        left = Inches(0.65 + i * 4.1)
        card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, Inches(1.35), Inches(3.55), Inches(1.25))
        card.fill.solid()
        card.fill.fore_color.rgb = _rgb("F8FAFC")
        card.line.color.rgb = _rgb("D1D5DB")
        _add_textbox(slide, left + Inches(0.22), Inches(1.55), Inches(3), Inches(0.25), label, 11, True, "6B7280")
        _add_textbox(slide, left + Inches(0.22), Inches(1.86), Inches(3), Inches(0.45), value, 24, True, "0B1F33")
    _add_table(slide, item_summary, Inches(0.65), Inches(3.05), Inches(12.0), Inches(2.75), 9, max_rows=8)
    _add_footer(slide, "Auto Report summary table generated from columnbase data and reformatter specs.")

    pass_board, value_board = _score_board_tables(item_summary, detail_df)
    _add_score_board_slide(
        prs,
        "Score Board - Index 1",
        "Wafer-level pass score by ADDP/reformatted index",
        pass_board,
        "Index 1 example uses per-wafer pass rate against reformatter spec limits.",
    )
    _add_score_board_slide(
        prs,
        "Score Board - Index 2",
        "Wafer-level median value by ADDP/reformatted index",
        value_board,
        "Index 2 example uses per-wafer median values for the same report indexes.",
    )

    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_title(slide, "Measurement Data Sample", "Rows are trimmed for mail package size control")
    sample_cols = [
        col
        for col in ["fab_lot_id", "wafer_id", "step_id", "tkout_time", "eqp_id", "CHIP_X_POS", "CHIP_Y_POS"]
        if col in detail_df.columns
    ]
    item_cols = [col for col in item_summary.get("Item", pd.Series(dtype=str)).tolist() if col in detail_df.columns]
    _add_table(slide, detail_df[sample_cols + item_cols], Inches(0.55), Inches(1.25), Inches(12.25), Inches(5.35), 8, max_rows=16)
    _add_footer(slide, "Detailed raw data should remain in the source database; this deck keeps only mail-sized summary samples.")
    return prs


def build_report_artifacts(cfg, df_target, df_reformatter, target_lot, target_root, target_step_id, upload_date):
    item_summary = build_item_summary(df_target, df_reformatter)
    if item_summary.empty:
        raise ValueError("No reportable item columns were found in target data")

    html_limit = bytes_from_mb(cfg.get("html_limit_mb"), DEFAULT_HTML_LIMIT_BYTES)
    ppt_limit = bytes_from_mb(cfg.get("ppt_limit_mb"), DEFAULT_PPT_LIMIT_BYTES)

    ppt_name = f"{upload_date}-{cfg.get('prod', cfg.get('vehicle', 'PRODUCT'))}-{target_root}-Report.pptx"
    html_name = f"{upload_date}-{cfg.get('prod', cfg.get('vehicle', 'PRODUCT'))}-{target_root}-Report.html"
    ppt_path = os.path.join(cfg["mail_ppt_path"], ppt_name)
    html_path = os.path.join(cfg["html_path"], html_name)
    os.makedirs(cfg["mail_ppt_path"], exist_ok=True)
    os.makedirs(cfg["html_path"], exist_ok=True)

    html_content = build_mail_html(target_lot, target_step_id, item_summary, df_target, html_limit)
    with open(html_path, "w", encoding="utf-8") as file:
        file.write(html_content)

    prs = build_professional_ppt(
        target_lot=target_lot,
        target_root=target_root,
        target_step_id=target_step_id,
        vehicle=cfg.get("vehicle", "PRODUCT"),
        prod=cfg.get("prod", cfg.get("vehicle", "PRODUCT")),
        item_summary=item_summary,
        detail_df=df_target,
    )
    prs.save(ppt_path)

    html_size = os.path.getsize(html_path)
    ppt_size = os.path.getsize(ppt_path)
    if html_size > html_limit:
        raise ValueError(f"HTML report exceeds limit: {html_size} bytes")
    if ppt_size > ppt_limit:
        raise ValueError(f"PPT report exceeds limit: {ppt_size} bytes")

    return {
        "ppt_path": ppt_path,
        "ppt_name": ppt_name,
        "ppt_size": ppt_size,
        "html_path": html_path,
        "html_content": html_content,
        "html_size": html_size,
    }
