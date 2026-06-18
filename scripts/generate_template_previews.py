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
    rect(draw, (90, 55, 1190, 165), "#0b1f33")
    text(draw, (125, 84), "AUTO REPORT - LOT001", 34, "#ffffff", True)
    text(draw, (126, 128), "Step STEP001 | generated from fixture columnbase data", 17, "#cbd5e1")
    text(draw, (125, 205), "Summary", 24, "#0b1f33", True)
    draw_table(
        draw,
        125,
        250,
        ["Item", "Count", "Mean", "Min", "Max", "Pass Rate"],
        [["ADDP_ITEM_01", "27", "56.3", "51.5", "61.5", "100"]],
        col_w=160,
    )
    text(draw, (125, 392), "Data Sample", 24, "#0b1f33", True)
    draw_table(
        draw,
        125,
        437,
        ["Lot", "Wafer", "Step", "X", "Y", "ADDP_ITEM_01"],
        [["LOT001", "1", "STEP001", "-1", "-1", "52.0"], ["LOT001", "2", "STEP001", "0", "1", "56.5"], ["LOT001", "3", "STEP001", "1", "0", "61.0"]],
        col_w=160,
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
    print(f"created previews in {PREVIEW_DIR}")


if __name__ == "__main__":
    main()
