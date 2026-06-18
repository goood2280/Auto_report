import ast
import operator
import os
import re
from datetime import datetime
from html import escape
from pathlib import Path

import numpy as np
import pandas as pd
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
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


def _numeric_text(value):
    if value == "" or pd.isna(value):
        return ""
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.3g}"
    return str(value)


def _score_class(value):
    try:
        number = float(value)
    except Exception:
        return ""
    if number >= 95:
        return "score-good"
    if number >= 80:
        return "score-warn"
    return "score-bad"


def _html_table(df, class_name, max_rows=20):
    display = df.head(max_rows).copy()
    return display.to_html(index=False, border=0, classes=class_name, na_rep="", escape=True)


def _score_board_html(df, max_rows=20):
    display = df.head(max_rows).copy()
    header = "".join(f"<th>{escape(str(col))}</th>" for col in display.columns)
    body_rows = []
    for _, row in display.iterrows():
        cells = []
        for col_idx, col in enumerate(display.columns):
            text = escape(_numeric_text(row[col]))
            css = _score_class(row[col]) if col_idx > 0 else "index-cell"
            class_attr = f' class="{css}"' if css else ""
            cells.append(f"<td{class_attr}>{text}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return '<table class="scoreboard"><thead><tr>' + header + "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>"


def _history_table(target_lot, target_step_id, item_summary, detail_df):
    time_col = next((col for col in ["tkout_time", "TKOUT_TIME", "date", "Date"] if col in detail_df.columns), None)
    wafer_count = int(detail_df["wafer_id"].nunique()) if "wafer_id" in detail_df.columns else len(detail_df)
    avg_pass = item_summary["Pass Rate"].mean() if "Pass Rate" in item_summary else np.nan
    if time_col:
        time_values = pd.to_datetime(detail_df[time_col], errors="coerce").dropna()
        history_time = time_values.max().strftime("%Y-%m-%d %H:%M") if not time_values.empty else str(detail_df[time_col].dropna().iloc[0])
    else:
        history_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    return pd.DataFrame(
        [
            {
                "History Time": history_time,
                "Lot": target_lot,
                "Step": target_step_id,
                "Wafers": wafer_count,
                "Measured Sites": len(detail_df),
                "Index Count": len(item_summary),
                "Avg Pass Rate": round(float(avg_pass), 1) if not np.isnan(avg_pass) else "",
            }
        ]
    )


def _item_stats_table(item, item_summary, detail_df):
    spec = item_summary[item_summary["Item"] == item].iloc[0]
    low = _safe_number(spec.get("Spec Low"))
    high = _safe_number(spec.get("Spec High"))
    rows = []
    groups = [("Overall", detail_df)]
    if "wafer_id" in detail_df.columns:
        for wafer, wafer_df in detail_df.groupby("wafer_id"):
            label = f"W{int(wafer):02d}" if isinstance(wafer, (int, float, np.integer, np.floating)) else f"W{wafer}"
            groups.append((label, wafer_df))
    for label, group in groups[:13]:
        values = pd.to_numeric(group[item], errors="coerce").dropna()
        if values.empty:
            continue
        pass_mask = pd.Series(True, index=values.index)
        if not np.isnan(low):
            pass_mask &= values >= low
        if not np.isnan(high):
            pass_mask &= values <= high
        rows.append(
            {
                "Scope": label,
                "N": int(values.count()),
                "Mean": round(float(values.mean()), 3),
                "Std": round(float(values.std(ddof=0)), 3),
                "Median": round(float(values.median()), 3),
                "Min": round(float(values.min()), 3),
                "Max": round(float(values.max()), 3),
                "Pass Rate": round(float(pass_mask.mean() * 100), 1),
            }
        )
    return pd.DataFrame(rows)


def _item_series_by_wafer(item, detail_df):
    if "wafer_id" not in detail_df.columns or item not in detail_df.columns:
        return []
    series = []
    for wafer, wafer_df in detail_df.groupby("wafer_id"):
        values = pd.to_numeric(wafer_df[item], errors="coerce").dropna()
        if values.empty:
            continue
        label = f"W{int(wafer):02d}" if isinstance(wafer, (int, float, np.integer, np.floating)) else f"W{wafer}"
        series.append((label, values))
    return series


def _value_scale(values, low=None, high=None):
    numeric = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if numeric.empty:
        return 0.0, 1.0
    min_v = float(numeric.min() if low is None or np.isnan(low) else min(numeric.min(), low))
    max_v = float(numeric.max() if high is None or np.isnan(high) else max(numeric.max(), high))
    if min_v == max_v:
        min_v -= 1.0
        max_v += 1.0
    pad = (max_v - min_v) * 0.08
    return min_v - pad, max_v + pad


def _add_axis(slide, left, top, width, height, min_v, max_v):
    axis_color = _rgb("94A3B8")
    x_axis = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, left, top + height, left + width, top + height)
    y_axis = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, left, top, left, top + height)
    x_axis.line.color.rgb = axis_color
    y_axis.line.color.rgb = axis_color
    _add_textbox(slide, left - Inches(0.4), top - Inches(0.04), Inches(0.35), Inches(0.2), f"{max_v:.1f}", 7, False, "64748B")
    _add_textbox(slide, left - Inches(0.4), top + height - Inches(0.08), Inches(0.35), Inches(0.2), f"{min_v:.1f}", 7, False, "64748B")


def _y_from_value(value, min_v, max_v, top, height):
    return top + height - ((float(value) - min_v) / (max_v - min_v) * height)


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


def _add_statistical_table_slide(prs, item, index_no, item_summary, detail_df):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_title(slide, f"Statistical Table - Index {index_no}", f"{item} wafer-level distribution summary")
    stats_df = _item_stats_table(item, item_summary, detail_df)
    _add_table(slide, stats_df, Inches(0.65), Inches(1.25), Inches(12.0), Inches(4.8), 8, max_rows=13)
    _add_footer(slide, "Template page: statistical table by index and wafer.")
    return slide


def _add_box_plot_slide(prs, item, index_no, item_summary, detail_df):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_title(slide, f"Box Plot - Index {index_no}", f"{item} distribution by wafer")
    wafer_series = _item_series_by_wafer(item, detail_df)[:10]
    all_values = [value for _, values in wafer_series for value in values.tolist()]
    spec = item_summary[item_summary["Item"] == item].iloc[0]
    min_v, max_v = _value_scale(all_values, _safe_number(spec.get("Spec Low")), _safe_number(spec.get("Spec High")))
    left, top, width, height = Inches(0.9), Inches(1.45), Inches(11.4), Inches(4.6)
    _add_axis(slide, left, top, width, height, min_v, max_v)

    if wafer_series:
        gap = width / max(len(wafer_series), 1)
        box_w = min(Inches(0.55), gap * 0.45)
        for idx, (label, values) in enumerate(wafer_series):
            q1, median, q3 = values.quantile([0.25, 0.5, 0.75]).tolist()
            low = float(values.min())
            high = float(values.max())
            x = left + gap * (idx + 0.5)
            y_low = _y_from_value(low, min_v, max_v, top, height)
            y_high = _y_from_value(high, min_v, max_v, top, height)
            y_q1 = _y_from_value(q1, min_v, max_v, top, height)
            y_q3 = _y_from_value(q3, min_v, max_v, top, height)
            y_med = _y_from_value(median, min_v, max_v, top, height)
            whisker = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, x, y_high, x, y_low)
            whisker.line.color.rgb = _rgb("475569")
            box = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x - box_w / 2, y_q3, box_w, max(Inches(0.06), y_q1 - y_q3))
            box.fill.solid()
            box.fill.fore_color.rgb = _rgb("DBEAFE")
            box.line.color.rgb = _rgb("2563EB")
            median_line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, x - box_w / 2, y_med, x + box_w / 2, y_med)
            median_line.line.color.rgb = _rgb("0F172A")
            _add_textbox(slide, x - Inches(0.22), top + height + Inches(0.12), Inches(0.45), Inches(0.22), label, 7, False, "64748B")
    _add_footer(slide, "Template page: box plot by index across wafers.")
    return slide


def _add_trend_slide(prs, item, index_no, item_summary, detail_df):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_title(slide, f"Trend - Index {index_no}", f"{item} wafer median trend")
    wafer_series = _item_series_by_wafer(item, detail_df)[:12]
    medians = [float(values.median()) for _, values in wafer_series]
    spec = item_summary[item_summary["Item"] == item].iloc[0]
    min_v, max_v = _value_scale(medians, _safe_number(spec.get("Spec Low")), _safe_number(spec.get("Spec High")))
    left, top, width, height = Inches(0.9), Inches(1.45), Inches(11.4), Inches(4.6)
    _add_axis(slide, left, top, width, height, min_v, max_v)

    points = []
    if wafer_series:
        gap = width / max(len(wafer_series) - 1, 1)
        for idx, ((label, _), value) in enumerate(zip(wafer_series, medians)):
            x = left + gap * idx if len(wafer_series) > 1 else left + width / 2
            y = _y_from_value(value, min_v, max_v, top, height)
            points.append((x, y, label, value))
        for (x1, y1, _, _), (x2, y2, _, _) in zip(points, points[1:]):
            line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, x1, y1, x2, y2)
            line.line.color.rgb = _rgb("00A3A3")
            line.line.width = Pt(2)
        for x, y, label, value in points:
            dot = slide.shapes.add_shape(MSO_SHAPE.OVAL, x - Inches(0.06), y - Inches(0.06), Inches(0.12), Inches(0.12))
            dot.fill.solid()
            dot.fill.fore_color.rgb = _rgb("00A3A3")
            dot.line.color.rgb = _rgb("0F766E")
            _add_textbox(slide, x - Inches(0.25), top + height + Inches(0.12), Inches(0.5), Inches(0.22), label, 7, False, "64748B")
            _add_textbox(slide, x - Inches(0.25), y - Inches(0.32), Inches(0.5), Inches(0.2), f"{value:.1f}", 7, False, "334155")
    _add_footer(slide, "Template page: median trend by index and wafer order.")
    return slide


def _add_wf_map_slide(prs, item, index_no, item_summary, detail_df):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_title(slide, f"WF Map - Index {index_no}", f"{item} wafer map template")
    coord_cols = [col for col in ["CHIP_X_POS", "CHIP_Y_POS"] if col in detail_df.columns]
    map_df = detail_df.copy()
    if "wafer_id" in map_df.columns:
        first_wafer = sorted(map_df["wafer_id"].dropna().unique().tolist())[0]
        map_df = map_df[map_df["wafer_id"] == first_wafer]

    left, top = Inches(4.25), Inches(1.2)
    cell = Inches(0.45)
    if len(coord_cols) == 2 and item in map_df.columns:
        x_vals = sorted(map_df["CHIP_X_POS"].dropna().unique().tolist())
        y_vals = sorted(map_df["CHIP_Y_POS"].dropna().unique().tolist(), reverse=True)
        values = pd.to_numeric(map_df[item], errors="coerce")
        min_v, max_v = _value_scale(values)
        for _, row in map_df.iterrows():
            x_idx = x_vals.index(row["CHIP_X_POS"])
            y_idx = y_vals.index(row["CHIP_Y_POS"])
            value = _safe_number(row[item])
            ratio = 0.5 if np.isnan(value) else (value - min_v) / (max_v - min_v)
            if ratio >= 0.66:
                color = "DCFCE7"
            elif ratio >= 0.33:
                color = "FEF9C3"
            else:
                color = "FEE2E2"
            chip = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left + x_idx * cell, top + y_idx * cell, cell, cell)
            chip.fill.solid()
            chip.fill.fore_color.rgb = _rgb(color)
            chip.line.color.rgb = _rgb("CBD5E1")
            _add_textbox(slide, left + x_idx * cell, top + y_idx * cell + Inches(0.13), cell, Inches(0.16), f"{value:.0f}", 7, False, "334155")
        _add_textbox(slide, Inches(0.7), Inches(1.55), Inches(2.9), Inches(0.45), "Color scale follows the selected index value range for the displayed wafer.", 11, False, "475569")
        _add_textbox(slide, Inches(0.7), Inches(2.15), Inches(2.9), Inches(0.3), f"Map chips: {len(map_df)}", 14, True, "0B1F33")
    else:
        _add_textbox(slide, Inches(0.7), Inches(2.3), Inches(10.5), Inches(0.5), "Coordinate columns are required for the WF map template.", 16, False, "475569")
    _add_footer(slide, "Template page: WF map by index using chip coordinates.")
    return slide


def _add_index_analysis_template_slides(prs, item_summary, detail_df, max_indexes=1):
    items = [item for item in item_summary.get("Item", pd.Series(dtype=str)).tolist() if item in detail_df.columns]
    for index_no, item in enumerate(items[:max_indexes], start=1):
        _add_statistical_table_slide(prs, item, index_no, item_summary, detail_df)
        _add_box_plot_slide(prs, item, index_no, item_summary, detail_df)
        _add_trend_slide(prs, item, index_no, item_summary, detail_df)
        _add_wf_map_slide(prs, item, index_no, item_summary, detail_df)


def build_mail_html(target_lot, target_step_id, item_summary, detail_df, max_bytes=DEFAULT_HTML_LIMIT_BYTES):
    pass_board, _ = _score_board_tables(item_summary, detail_df)
    history_df = _history_table(target_lot, target_step_id, item_summary, detail_df)

    style = """
<style>
body{font-family:Arial,sans-serif;color:#1f2937;margin:0;padding:16px;background:#f8fafc}
.wrap{max-width:1180px;margin:0 auto;background:#fff;border:1px solid #e5e7eb}
.head{background:#0b1f33;color:#fff;padding:18px 22px}
.head h1{font-size:22px;margin:0 0 4px}.head p{font-size:12px;margin:0;color:#cbd5e1}
.section{padding:14px 22px}.section h2{font-size:16px;margin:0 0 8px;color:#0b1f33}
table{border-collapse:collapse;width:100%;font-size:12px}th{background:#0b1f33;color:#fff}
th,td{border:1px solid #e5e7eb;padding:5px 7px;text-align:center}tr:nth-child(even){background:#f8fafc}
.index-cell{font-weight:700;text-align:left}.score-good{background:#dcfce7}.score-warn{background:#fef9c3}.score-bad{background:#fee2e2}
.subnote{font-size:11px;color:#64748b;margin:0 0 8px}.legend{font-size:11px;color:#64748b;margin-top:6px}
.note{font-size:11px;color:#6b7280;padding:12px 22px 18px}
</style>
"""
    row_limit = min(max(len(item_summary), 1), 80)
    while row_limit >= 1:
        score_html = _score_board_html(pass_board, max_rows=row_limit)
        inline_html = _html_table(item_summary, "inline-table", max_rows=row_limit)
        history_html = _html_table(history_df, "history", max_rows=12)
        html = f"""<!doctype html><html><head><meta charset="utf-8">{style}</head><body>
<div class="wrap">
<div class="head"><h1>AUTO REPORT - {target_lot}</h1><p>Step {target_step_id} | generated {datetime.now():%Y-%m-%d %H:%M}</p></div>
<div class="section"><h2>Score Board</h2><p class="subnote">Wafer-level pass rate by report index.</p>{score_html}<div class="legend">green &gt;=95, yellow 80-94.9, red &lt;80</div></div>
<div class="section"><h2>Inline Table</h2><p class="subnote">Inline item summary for mail review.</p>{inline_html}</div>
<div class="section"><h2>History</h2><p class="subnote">Latest report run context from the available columnbase rows.</p>{history_html}</div>
<div class="note">This is an automated mail summary. The attached PPT contains the report package.</div>
</div></body></html>"""
        if len(html.encode("utf-8")) <= max_bytes:
            return html
        row_limit //= 2
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
    _add_index_analysis_template_slides(prs, item_summary, detail_df, max_indexes=1)
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
