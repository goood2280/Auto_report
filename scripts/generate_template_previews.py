from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
PREVIEW_DIR = ROOT / "templates" / "previews"
WIDTH, HEIGHT = 1280, 720


def font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def save(img, name):
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    img.save(PREVIEW_DIR / name, optimize=True)


def rect(draw, xy, fill, outline=None, width=1):
    draw.rectangle(xy, fill=fill, outline=outline, width=width)


def text(draw, xy, value, size=24, fill="#1f2937", bold=False):
    draw.text(xy, str(value), font=font(size, bold), fill=fill)


def draw_table(draw, x, y, columns, rows, col_w=130, row_h=42, header="#0b1f33"):
    for c, col in enumerate(columns):
        x0 = x + c * col_w
        rect(draw, (x0, y, x0 + col_w, y + row_h), header, "#d1d5db")
        text(draw, (x0 + 10, y + 11), col, 16, "#ffffff", True)
    for r, row in enumerate(rows):
        y0 = y + (r + 1) * row_h
        bg = "#f8fafc" if r % 2 == 0 else "#ffffff"
        for c, value in enumerate(row):
            x0 = x + c * col_w
            cell_bg = bg
            if c > 0:
                try:
                    number = float(value)
                    if number >= 95:
                        cell_bg = "#dcfce7"
                    elif number >= 80:
                        cell_bg = "#fef9c3"
                    else:
                        cell_bg = "#fee2e2"
                except Exception:
                    pass
            rect(draw, (x0, y0, x0 + col_w, y0 + row_h), cell_bg, "#d1d5db")
            text(draw, (x0 + 10, y0 + 11), value, 15, "#1f2937", c == 0)


def html_preview():
    img = Image.new("RGB", (WIDTH, HEIGHT), "#f8fafc")
    draw = ImageDraw.Draw(img)
    rect(draw, (90, 55, 1190, 665), "#ffffff", "#e5e7eb")
    rect(draw, (90, 55, 1190, 145), "#0b1f33")
    text(draw, (125, 82), "AUTO REPORT - LOT001", 30, "#ffffff", True)
    text(draw, (126, 120), "Step STEP001 | mail body template", 15, "#cbd5e1")
    text(draw, (125, 178), "Score Board", 22, "#0b1f33", True)
    draw_table(
        draw,
        125,
        218,
        ["Index", "W01", "W02", "W03"],
        [["ADDP_ITEM_01", "100", "100", "100"]],
        col_w=235,
    )
    text(draw, (125, 338), "Inline Table", 22, "#0b1f33", True)
    draw_table(
        draw,
        125,
        378,
        ["Item", "Count", "Mean", "Min", "Max", "Pass Rate"],
        [["ADDP_ITEM_01", "27", "62.0", "51.0", "73.0", "100"]],
        col_w=160,
    )
    text(draw, (125, 497), "History", 22, "#0b1f33", True)
    draw_table(
        draw,
        125,
        537,
        ["History Time", "Lot", "Step", "Wafers", "Avg Pass"],
        [["2026-01-01 00:00", "LOT001", "STEP001", "3", "100"]],
        col_w=190,
    )
    save(img, "template_report_html.png")


def ppt_cover_preview():
    img = Image.new("RGB", (WIDTH, HEIGHT), "#0b1f33")
    draw = ImageDraw.Draw(img)
    rect(draw, (0, 657, WIDTH, HEIGHT), "#00a3a3")
    text(draw, (84, 150), "VEHICLE_A Auto Report", 50, "#ffffff", True)
    text(draw, (88, 245), "LOT001 | STEP001 | PRODUCT_NAME_A", 26, "#cbd5e1")
    text(draw, (92, 572), "PowerPoint mail package preview", 18, "#e5e7eb")
    save(img, "template_report_ppt_cover.png")


def scoreboard_preview(name, title, subtitle, rows):
    img = Image.new("RGB", (WIDTH, HEIGHT), "#ffffff")
    draw = ImageDraw.Draw(img)
    text(draw, (54, 34), title, 34, "#0b1f33", True)
    rect(draw, (55, 90, 180, 98), "#00a3a3")
    text(draw, (205, 83), subtitle, 16, "#6b7280")
    draw_table(draw, 70, 150, ["Index", "W01", "W02", "W03", "W04", "W05", "W06"], rows, col_w=155)
    rect(draw, (70, 632, 96, 650), "#dcfce7", "#cbd5e1")
    text(draw, (108, 627), ">=95", 15, "#6b7280")
    rect(draw, (190, 632, 216, 650), "#fef9c3", "#cbd5e1")
    text(draw, (228, 627), "80-94.9", 15, "#6b7280")
    rect(draw, (330, 632, 356, 650), "#fee2e2", "#cbd5e1")
    text(draw, (368, 627), "<80", 15, "#6b7280")
    save(img, name)


def statistical_table_preview():
    img = Image.new("RGB", (WIDTH, HEIGHT), "#ffffff")
    draw = ImageDraw.Draw(img)
    text(draw, (54, 34), "Statistical Table - Index 1", 34, "#0b1f33", True)
    rect(draw, (55, 90, 180, 98), "#00a3a3")
    text(draw, (205, 83), "ADDP_ITEM_01 wafer-level distribution summary", 16, "#6b7280")
    draw_table(
        draw,
        70,
        150,
        ["Scope", "N", "Mean", "Std", "Median", "Min", "Max", "Pass"],
        [["Overall", "27", "62.0", "8.18", "62.0", "51.0", "73.0", "100"], ["W01", "9", "52.0", "0.58", "52.0", "51.0", "53.0", "100"], ["W02", "9", "62.0", "0.58", "62.0", "61.0", "63.0", "100"]],
        col_w=140,
    )
    save(img, "template_report_statistical_table_index_1.png")


def box_plot_preview():
    img = Image.new("RGB", (WIDTH, HEIGHT), "#ffffff")
    draw = ImageDraw.Draw(img)
    text(draw, (54, 34), "Box Plot - Index 1", 34, "#0b1f33", True)
    rect(draw, (55, 90, 180, 98), "#00a3a3")
    text(draw, (205, 83), "ADDP_ITEM_01 distribution by wafer", 16, "#6b7280")
    chart = (110, 150, 1160, 610)
    rect(draw, chart, "#ffffff", "#cbd5e1")
    for x, label, y1, y2 in [(300, "W01", 420, 480), (620, "W02", 315, 375), (940, "W03", 205, 265)]:
        draw.line((x, y1 - 45, x, y2 + 45), fill="#475569", width=3)
        rect(draw, (x - 50, y1, x + 50, y2), "#dbeafe", "#2563eb", 2)
        draw.line((x - 50, (y1 + y2) // 2, x + 50, (y1 + y2) // 2), fill="#0f172a", width=3)
        text(draw, (x - 28, 625), label, 16, "#64748b")
    save(img, "template_report_box_plot_index_1.png")


def trend_preview():
    img = Image.new("RGB", (WIDTH, HEIGHT), "#ffffff")
    draw = ImageDraw.Draw(img)
    text(draw, (54, 34), "Trend - Index 1", 34, "#0b1f33", True)
    rect(draw, (55, 90, 180, 98), "#00a3a3")
    text(draw, (205, 83), "ADDP_ITEM_01 wafer median trend", 16, "#6b7280")
    chart = (110, 150, 1160, 610)
    rect(draw, chart, "#ffffff", "#cbd5e1")
    points = [(220, 470, "W01", "52.0"), (620, 340, "W02", "62.0"), (1020, 210, "W03", "72.0")]
    for p1, p2 in zip(points, points[1:]):
        draw.line((p1[0], p1[1], p2[0], p2[1]), fill="#00a3a3", width=4)
    for x, y, label, value in points:
        draw.ellipse((x - 8, y - 8, x + 8, y + 8), fill="#00a3a3", outline="#0f766e")
        text(draw, (x - 24, y - 38), value, 15, "#334155")
        text(draw, (x - 24, 625), label, 16, "#64748b")
    save(img, "template_report_trend_index_1.png")


def wafer_map_preview():
    img = Image.new("RGB", (WIDTH, HEIGHT), "#ffffff")
    draw = ImageDraw.Draw(img)
    text(draw, (54, 34), "WF Map - Index 1", 34, "#0b1f33", True)
    rect(draw, (55, 90, 180, 98), "#00a3a3")
    text(draw, (205, 83), "ADDP_ITEM_01 wafer map template", 16, "#6b7280")
    text(draw, (90, 170), "Map chips: 9", 24, "#0b1f33", True)
    text(draw, (90, 220), "Color scale follows the selected index value range.", 17, "#64748b")
    start_x, start_y, size = 560, 180, 90
    values = [["51", "52", "53"], ["61", "62", "63"], ["71", "72", "73"]]
    colors = [["#fee2e2", "#fee2e2", "#fef9c3"], ["#fef9c3", "#fef9c3", "#fef9c3"], ["#dcfce7", "#dcfce7", "#dcfce7"]]
    for r in range(3):
        for c in range(3):
            x, y = start_x + c * size, start_y + r * size
            rect(draw, (x, y, x + size, y + size), colors[r][c], "#cbd5e1", 2)
            text(draw, (x + 28, y + 33), values[r][c], 20, "#334155", True)
    save(img, "template_report_wafer_map_index_1.png")


def main():
    html_preview()
    ppt_cover_preview()
    scoreboard_preview(
        "template_report_scoreboard_index_1.png",
        "Score Board - Index 1",
        "Wafer-level pass score by ADDP/reformatted index",
        [["ADDP_ITEM_01", "100", "100", "100", "", "", ""], ["ADDP_ITEM_02", "92.5", "96.0", "87.5", "", "", ""]],
    )
    scoreboard_preview(
        "template_report_scoreboard_index_2.png",
        "Score Board - Index 2",
        "Wafer-level median value by ADDP/reformatted index",
        [["ADDP_ITEM_01", "52.0", "56.5", "61.0", "", "", ""], ["ADDP_ITEM_02", "8.2", "7.9", "8.6", "", "", ""]],
    )
    statistical_table_preview()
    box_plot_preview()
    trend_preview()
    wafer_map_preview()
    print(f"created previews in {PREVIEW_DIR}")


if __name__ == "__main__":
    main()
