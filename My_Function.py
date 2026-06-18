import os  
import re  
import time  
import hashlib  
from datetime import datetime, timedelta

import numpy as np  
import pandas as pd

from PIL import Image

from pptx import Presentation  
from pptx.util import Inches, Pt  
from pptx.enum.text import PP_ALIGN  
from pptx.enum.shapes import MSO_CONNECTOR  
from pptx.dml.color import RGBColor

import matplotlib.pyplot as plt  
import seaborn as sns  
from io import BytesIO

from matplotlib.colors import BoundaryNorm  
from matplotlib.colors import LinearSegmentedColormap  
from matplotlib.cm import ScalarMappable  
import matplotlib.lines as mlines  
import matplotlib.dates as mdates  
from matplotlib.patches import Patch  
from matplotlib.ticker import FuncFormatter, NullFormatter, NullLocator, FixedLocator

from internal_modules import *

COL_ID = 'col_id'  
COL_TIME = 'col_time'  
COL_VALUE = 'col_value'  
COL_WAFER = 'col_wafer'  
COL_STEP = 'col_step'  
COL_SPEC_MIN = 'col_spec_min'  
COL_SPEC_MAX = 'col_spec_max'  
COL_TARGET = 'col_target'  
COL_UNIT = 'col_unit'  
COL_DIRECTION = 'col_direction'  
COL_CATEGORY = 'col_category'  
COL_ALIAS = 'col_alias'  
COL_ORDER = 'col_order'

PATH_CONFIG = 'path_config'  
PATH_DB = 'path_db'  
TABLE_ET = 'table_et'  
TABLE_FAB = 'table_fab'  
TABLE_WIP = 'table_wip'

def generate_sha256_key(row, columns_to_use):  
    combined_str = ''.join(str(row[col]) for col in columns_to_use)  
    sha256_hash = hashlib.sha256(combined_str.encode()).hexdigest()  
    return sha256_hash

def log_to_file(message, log_file):  
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  
    log_message = f"{current_time} {message}\n"  
    with open(log_file, 'a') as file:  
        file.write(log_message)

def get_email_list(file_path, target_group, default_group='DEFAULT', domain="@domain.com"):  
    xls = pd.ExcelFile(file_path)  
    sheet_name = target_group if target_group in xls.sheet_names else default_group  
    df = xls.parse(sheet_name)  
    email_list = df[COL_ID].dropna().drop_duplicates(keep='first').tolist()  
    domain = domain.strip()  
    final_email_list = [  
        {  
        "email": email.strip() if "@" in email else f"{email.strip()}{domain}",  
        "recipientType": "TO",  
        "seq": i  
        }  
        for i, email in enumerate(email_list, start=1)  
    ]  
    return final_email_list

def make_title_page(template_ppt_path, vehicle, target_lot_id, target_DC_step, slide_width_=18, slide_height_=9):  
    prs = Presentation(template_ppt_path)  
    prs.slide_width = Inches(slide_width_)  
    prs.slide_height = Inches(slide_height_)  
    first_slide = prs.slides[0]  
    for shape in first_slide.shapes:  
        try:  
            text_frame = shape.text_frame  
            text_ = text_frame.text  
            if 'Template' in text_:  
                text_frame.clear()  
                p = text_frame.paragraphs[0]  
                p.text = f"{vehicle} {target_lot_id} {target_DC_step}\nAuto Report"  
                p.font.bold = True  
                p.font.size = Pt(64)  
                p.font.name = 'Arial'  
            elif 'YYYY' in text_ and 'MM' in text_ and 'DD' in text_:  
                text_frame.clear()  
                p = text_frame.paragraphs[0]  
                p.text = datetime.now().strftime('%Y-%m-%d')  
                p.font.size = Pt(24)  
        except:  
            pass  
    return prs

def calcaulate_description_image_info_dict(description_ppt_path, img_quality=100):  
    prs = Presentation(description_ppt_path)  
    description_image_info_dict = {}  
    for slide in prs.slides:  
        title_text = None  
        max_size = 0  
        largest_image_info = None  
        for shape in slide.shapes:  
            if not title_text and shape.has_text_frame and shape.text_frame.text:  
                title_text = shape.text_frame.text  
            if shape.shape_type == 13:  
                image_size = shape.width * shape.height  
                if image_size > max_size:  
                    max_size = image_size  
                    image_stream = BytesIO(shape.image.blob)  
                    with Image.open(image_stream) as img:  
                        if img.mode != "RGBA":  
                            img = img.convert("RGBA")  
                        rgb_img = Image.new("RGB", img.size, (255, 255, 255))  
                        channels = img.split()  
                        if len(channels) == 4:  
                            rgb_img.paste(img, mask=channels[3])  
                        else:  
                            rgb_img.paste(img)  
                        final_stream = BytesIO()  
                        rgb_img.save(final_stream, format='JPEG', quality=img_quality)  
                        final_stream.seek(0)  
                    largest_image_info = {  
                        'stream': final_stream,  
                        'left': shape.left,  
                        'top': shape.top,  
                        'width': shape.width,  
                        'height': shape.height  
                    }  
        if not title_text:  
            title_text = 'No Title'  
        if largest_image_info:  
            description_image_info_dict[title_text] = largest_image_info  
    return description_image_info_dict

def insert_score_board(df, prs, target_lot_id, sub_name, header_font_size=12, index_font_size=11, value_font_size=11, index_size_n=7):  
    df.index = df.index.map(  
            lambda x: convert_target_data(  
                x,  
                CONFIG.get("suffixes_remove"),  
                CONFIG.get("replace_map")  
            )  
        )  
    num_rows, num_cols = 25, 25  
    slide_width = prs.slide_width  
    slide_height = prs.slide_height  
    margin = Inches(0.1)  
    title_space = Inches(1)  
    slide_height -= title_space  
    list_of_dfs = [df.iloc[i:i + num_rows] for i in range(0, len(df), num_rows)]  
    for chunk_df in list_of_dfs:  
        df_row_cnt, df_col_cnt = chunk_df.shape  
        slide_layout = prs.slide_layouts[6]  
        slide = prs.slides.add_slide(slide_layout)  
        title_box = slide.shapes.add_textbox(margin, margin, prs.slide_width - 2 * margin, title_space)  
        title_text_frame = title_box.text_frame  
        title_text_frame.text = f"{CONFIG.get('node')} {target_lot_id} Key Item Score"  
        title_text_frame.paragraphs[0].font.size = Pt(36)  
        title_text_frame.paragraphs[0].font.name = 'Arial Black'  
        title_text_frame.paragraphs[0].font.color.rgb = RGBColor(0, 0, 255)  
        cell_width = (slide_width - 2 * margin) // (num_cols + index_size_n)  
        cell_height = (slide_height - margin) // (num_rows + 1)  
        table = slide.shapes.add_table(num_rows + 1, num_cols + index_size_n, margin, title_space, slide_width - 2 * margin, slide_height - margin).table  
        for i in range(num_rows + 1):  
            for j in range(num_cols + index_size_n):  
                cell = table.cell(i, j)  
                cell.width = cell_width  
                cell.height = cell_height  
                para = cell.text_frame.paragraphs[0]  
                para.margin_top = 0  
                para.margin_bottom = 0  
                para.alignment = PP_ALIGN.CENTER  
                wf_num = int(j-index_size_n+1)  
                if i == 0:  
                    if j > 0:  
                        para.font.size = Pt(header_font_size)  
                        para.text = f"#{wf_num}" if wf_num >= 1 else ''  
                        para.font.color.rgb = RGBColor(255, 255, 255)  
                    else:  
                        para.font.size = Pt(header_font_size+2)  
                        para.text = sub_name  
                        para.font.color.rgb = RGBColor(0, 0, 0)  
                    para.font.bold = True  
                    cell.fill.solid()  
                    cell.fill.fore_color.rgb = RGBColor(0, 0, 0) if wf_num >= 1 else RGBColor(255, 255, 255)  
                elif j == 0:  
                    value = str(chunk_df.index[i-1]) if i <= df_row_cnt else ''  
                    para.font.size = Pt(index_font_size if len(value) < 35 else index_font_size-1 if len(value) < 40 else index_font_size-2)  
                    para.font.bold = True  
                    para.text = value  
                    cell.fill.solid()  
                    cell.fill.fore_color.rgb = RGBColor(200, 200, 200)  
                else:  
                    value = chunk_df.iloc[i-1][wf_num] if wf_num >= 1 and wf_num <= 25 and wf_num in chunk_df and i <= df_row_cnt else float('nan')  
                    para.font.size = Pt(value_font_size)  
                    para.font.bold = True  
                    if pd.isna(value):  
                        para.text = ''  
                        cell.fill.solid()  
                        cell.fill.fore_color.rgb = RGBColor(200, 200, 200)  
                    else:  
                        para.text = f"{int(value)}"  
                        cell.fill.solid()  
                        if value < 50:  
                            cell.fill.fore_color.rgb = RGBColor(255, 0, 0)  
                        elif value < 80:  
                            cell.fill.fore_color.rgb = RGBColor(255, 165, 0)  
                        elif value < 100:  
                            cell.fill.fore_color.rgb = RGBColor(255, 255, 0)  
                        else:  
                            cell.fill.fore_color.rgb = RGBColor(0, 128, 0)  
        for row in range(num_rows + 1):  
            first_row = table.rows[row]  
            first_row.cells[0].merge(first_row.cells[index_size_n-1])  
    return prs



def insert_rawdata_board(df, prs, target_lot_id, sub_name, header_font_size=12, index_font_size=11, value_font_size=11, index_size_n=7):  
    num_rows, num_cols = 25, 25  
    slide_width = prs.slide_width  
    slide_height = prs.slide_height  
    margin = Inches(0.1)  
    title_space = Inches(1)  
    slide_height -= title_space  
    list_of_dfs = [df.iloc[i:i + num_rows] for i in range(0, len(df), num_rows)]  
    for chunk_df in list_of_dfs:  
        df_row_cnt, df_col_cnt = chunk_df.shape  
        slide_layout = prs.slide_layouts[6]  
        slide = prs.slides.add_slide(slide_layout)  
        title_box = slide.shapes.add_textbox(margin, margin, prs.slide_width - 2 * margin, title_space)  
        title_text_frame = title_box.text_frame  
        title_text_frame.text = f"{target_lot_id} Key Item Raw data"  
        title_text_frame.paragraphs[0].font.size = Pt(36)  
        title_text_frame.paragraphs[0].font.name = 'Arial Black'  
        title_text_frame.paragraphs[0].font.color.rgb = RGBColor(0, 0, 255)  
        cell_width = (slide_width - 2 * margin) // (num_cols + index_size_n)  
        cell_height = (slide_height - margin) // (num_rows + 1)  
        table = slide.shapes.add_table(num_rows + 1, num_cols + index_size_n, margin, title_space, slide_width - 2 * margin, slide_height - margin).table  
        for i in range(num_rows + 1):  
            for j in range(num_cols + index_size_n):  
                cell = table.cell(i, j)  
                cell.width = cell_width  
                cell.height = cell_height  
                para = cell.text_frame.paragraphs[0]  
                para.margin_top = 0  
                para.margin_bottom = 0  
                para.alignment = PP_ALIGN.CENTER  
                wf_num = int(j-index_size_n+1)  
                if i == 0:  
                    if j > 0:  
                        para.font.size = Pt(header_font_size)  
                        para.text = f"#{wf_num}" if wf_num >= 1 else ''  
                        para.font.color.rgb = RGBColor(255, 255, 255)  
                    else:  
                        para.font.size = Pt(header_font_size+2)  
                        para.text = sub_name  
                        para.font.color.rgb = RGBColor(0, 0, 0)  
                    para.font.bold = True  
                    cell.fill.solid()  
                    cell.fill.fore_color.rgb = RGBColor(0, 0, 0) if wf_num >= 1 else RGBColor(255, 255, 255)  
                elif j == 0:  
                    value = str(chunk_df.index[i-1]) if i <= df_row_cnt else ''  
                    para.font.size = Pt(index_font_size if len(value) < 35 else index_font_size-1 if len(value) < 40 else index_font_size-2)  
                    para.font.bold = True  
                    para.text = value  
                    cell.fill.solid()  
                    cell.fill.fore_color.rgb = RGBColor(200, 200, 200)  
                else:  
                    value = chunk_df.iloc[i-1][wf_num] if wf_num >= 1 and wf_num <= 25 and wf_num in chunk_df and i <= df_row_cnt else float('nan')  
                    para.font.size = Pt(value_font_size)  
                    para.font.bold = True  
                    if pd.isna(value):  
                        para.text = ''  
                        cell.fill.solid()  
                        cell.fill.fore_color.rgb = RGBColor(200, 200, 200)  
                    else:  
                        para.text  = '' if np.isnan(value) else "{:.3g}".format(float(value)) if 0.01 <= abs(value) < 10000 else "{:.2e}".format(float(value))  
        for row in range(num_rows + 1):  
            first_row = table.rows[row]  
            first_row.cells[0].merge(first_row.cells[index_size_n-1])  
    return prs

def convert_target_data(target_data: str, suffix_list=None, replace_map=None):  
    if suffix_list is None:  
        suffix_list = []  
    if replace_map is None:  
        replace_map = {}  
    for old, new in replace_map.items():  
        target_data = target_data.replace(old, new)  
    use_rmax = target_data.startswith("RMAX_")  
    if use_rmax:  
        base = target_data[len("RMAX_"):]  
    else:  
        base = target_data  
    if suffix_list:  
        pattern = r'(_?(' + "|".join(map(re.escape, suffix_list)) + r')_?)'  
        base = re.sub(pattern, "", base)  
        base = re.sub(r'__+', '_', base)  
        base = base.strip('_')  
    base = base.replace("_", " ")  
    if use_rmax:  
        return f"MAX({base})"  
    return base

def insert_plots(    
    item_df,    
    trend_df,    
    prs,    
    description_image_info_dict,    
    target_fab_lot_id,    
    target_lot_id,    
    target_DC_step,    
    target_DC_step_id,    
    spec_dict,    
    img_quality=100,    
    ref=False,    
    color_dict=None,    
):    
    if color_dict is None:  
        color_dict = {}

    def to_float(x):    
        if pd.isna(x):    
            return np.nan    
        try:    
            return float(x)    
        except Exception:    
            return np.nan

    match_key = f"{target_lot_id}_{target_DC_step_id}"    
    search_key = f"{target_fab_lot_id}_{target_DC_step_id}"    
    search_key_DC_step_ver = f"{target_fab_lot_id}_{target_DC_step}"

    df = item_df.copy()

    slide_width = prs.slide_width    
    slide_height = prs.slide_height    
    slide_width_tick = slide_width / 24    
    slide_height_tick = slide_height / 12

    margin = Inches(0.1)    
    title_space = Inches(1)    
    slide_height -= title_space    
    middle_of_slide = 0.65 * slide_width

    agg_funcs_dict_upper = {    
        "P95": lambda x: x.quantile(0.95),    
        "P90": lambda x: x.quantile(0.90),    
        "MED": lambda x: x.median(),    
        "P10": lambda x: x.quantile(0.10),    
    }    
    agg_funcs_dict_lower = {    
        "P90": lambda x: x.quantile(0.90),    
        "MED": lambda x: x.median(),    
        "P10": lambda x: x.quantile(0.10),    
        "P05": lambda x: x.quantile(0.05),    
    }    
    agg_funcs_dict_both = {    
        "P90": lambda x: x.quantile(0.90),    
        "MED": lambda x: x.median(),    
        "AVG": lambda x: x.mean(),    
        "P10": lambda x: x.quantile(0.10),    
    }

    x_list = [str(i) for i in range(1, 26)]

    df["WAFER_ID_str"] = df[COL_WAFER].apply(    
        lambda x: str(x) if isinstance(x, int) else x    
    )

    cols_to_drop = []    
    for col in df.columns:    
        ref_exists = df[df["WAFER_ID_str"] == "Ref."][col].notna().any()    
        nonref_na = df[df["WAFER_ID_str"] != "Ref."][col].isna().all()    
        if ref_exists and nonref_na:    
            cols_to_drop.append(col)    
    df.drop(columns=cols_to_drop, inplace=True, errors="ignore")

    item_index_table = pd.DataFrame()

    cat2 = ""    
    reformatter = pd.read_csv(f'{PATH_CONFIG}/{CONFIG.get("vehicle")}_reformatter.csv')  
    reformatter = reformatter.set_index(COL_ALIAS).sort_values(COL_ORDER)

    for alias in reformatter.index:    
        try:    
            if alias not in df:    
                continue

            target_data = alias    
            target_data_changed = convert_target_data(    
                target_data,    
                suffix_list=CONFIG.get("suffixes_remove"),    
                replace_map=CONFIG.get("replace_map"),    
            )    
            target_unit = spec_dict.loc[alias, COL_UNIT] if alias in spec_dict.index else ""  
            df[target_data] = df[alias]

            radius_cols = [c for c in item_df.columns if "Radius" in c]

            df_target = df.dropna(subset=[target_data]).copy()    
            df_target[target_data] = pd.to_numeric(    
                df_target[target_data], errors="coerce"    
            )

            direction = spec_dict.loc[alias, COL_DIRECTION] if alias in spec_dict.index else "BOTH"  
            if direction == "UPPER":    
                color_list = ["blue", "grey", "red"]    
            elif direction == "LOWER":    
                color_list = ["red", "grey", "blue"]    
            else:    
                color_list = ["yellow", "blue", "red"]    
            cmap = LinearSegmentedColormap.from_list(    
                "custom_color_list", color_list, N=10    
            )

            df_groups = df_target.groupby(    
                ["DC_Split", "TEMPERATURE", "FLAT_ZONE_POS"], observed=False    
            )

            for group_name, df_group in df_groups:    
                now_cat2 = spec_dict.loc[alias, "CAT2"] if alias in spec_dict.index else ""    
                if now_cat2 != cat2:    
                    slide = prs.slides.add_slide(prs.slide_layouts[5])    
                    title = slide.shapes.title    
                    title.text = now_cat2    
                    title.width, title.height = slide_width, Inches(1.75)    
                    title.text_frame.paragraphs[0].font.size = Pt(80)    
                    title.text_frame.paragraphs[0].font.name = "Arial Black"    
                    title.left, title.top = 0, 0

                    if now_cat2 in description_image_info_dict:    
                        img = description_image_info_dict[now_cat2]    
                        slide.shapes.add_picture(    
                            img["stream"], img["left"], img["top"], img["width"], img["height"]    
                        )    
                    cat2 = now_cat2

                group_name_str = ", ".join(    
                    [    
                        f"{val}°C"    
                        if i == 1    
                        else f"Rotation({val}°)"    
                        if i == 2    
                        else val    
                        for i, val in enumerate(group_name)    
                        if (i == 0)    
                        or (i == 1 and int(val) != 25)    
                        or (i == 2 and int(val) != 0)    
                    ]    
                )

                slide = prs.slides.add_slide(prs.slide_layouts[6])    
                title_box = slide.shapes.add_textbox(    
                    margin, margin, prs.slide_width - 2 * margin, title_space    
                )    
                tf = title_box.text_frame    
                tf.text = f"{target_data_changed}[{target_unit}]"    
                tf.paragraphs[0].font.size = Pt(30)    
                tf.paragraphs[0].font.name = "Arial"    
                tf.paragraphs[0].font.color.rgb = RGBColor(0, 0, 255)

                df_group[target_data] = pd.to_numeric(df_group[target_data], errors="coerce")

                fig, ax = plt.subplots(figsize=(15, 5))    
                sns.boxplot(    
                    x="WAFER_ID_str",    
                    y=target_data,    
                    hue="WAFER_ID_str",    
                    data=df_group,    
                    ax=ax,    
                    palette=color_dict,    
                    linewidth=2.5,    
                    fliersize=5,    
                    whis=1.5,  
                    order=x_list,    
                    legend=False,    
                )

                spec_low = to_float(    
                    spec_dict.loc[alias, COL_SPEC_MIN] if alias in spec_dict.index else np.nan    
                )    
                spec_high = to_float(    
                    spec_dict.loc[alias, COL_SPEC_MAX] if alias in spec_dict.index else np.nan    
                )    
                tg = to_float(spec_dict.loc[alias, COL_TARGET] if alias in spec_dict.index else np.nan)

                log_scale = bool(spec_dict.loc[alias, "REPORT LOG SCALE"]) if alias in spec_dict.index else False    
                if log_scale:    
                    ax.set_yscale("log")    
                      
                sns.boxplot(    
                    x="WAFER_ID_str",    
                    y=target_data,    
                    hue="WAFER_ID_str",    
                    data=df_group,    
                    ax=ax,    
                    palette=color_dict,    
                    linewidth=2.5,    
                    fliersize=5,    
                    whis=1.5,  
                    order=x_list,    
                    legend=False,    
                )

                report_direction = str(spec_dict.loc[alias, COL_DIRECTION]).upper() if alias in spec_dict.index else "BOTH"

                if report_direction == "UPPER":  
                    if not np.isnan(spec_high):  
                        ax.axhline(spec_high, color="red", linestyle="--", lw=2.5)

                elif report_direction == "LOWER":  
                    if not np.isnan(spec_low):  
                        ax.axhline(spec_low, color="red", linestyle="--", lw=2.5)

                else:  
                    if not np.isnan(spec_low):  
                        ax.axhline(spec_low, color="red", linestyle="--", lw=2.5)  
                    if not np.isnan(spec_high):  
                        ax.axhline(spec_high, color="red", linestyle="--", lw=2.5)

                ax.set_title(    
                    f"[{target_lot_id}, {group_name_str}] {target_data_changed}[{target_unit}]",    
                    fontsize=24,    
                )    
                ax.set_xlabel("wafer_id", fontsize=14)    
                ax.set_ylabel("")  
                ax.tick_params(axis="x", labelsize=14)    
                ax.grid(True, alpha=0.5, zorder=0)    
                plt.tight_layout()  

                img_buf = BytesIO()    
                fig.savefig(img_buf, format="JPEG", bbox_inches="tight")    
                img_buf.seek(0)    
                with Image.open(img_buf) as img:    
                    final_stream = BytesIO()    
                    img.save(final_stream, format="JPEG", quality=img_quality)    
                    final_stream.seek(0)    
                plt.close()

                slide.shapes.add_picture(    
                    final_stream,    
                    margin,    
                    title_space    
                    + 0.25 * (slide_height - title_space)    
                    + margin,    
                    middle_of_slide - margin,    
                    0.5 * slide_height - margin,    
                )

                is_window = "Window" in alias

                fig, ax = plt.subplots(figsize=(9, 4.5))

                if not trend_df.empty:  
                    for i, vehicle_wv in enumerate(trend_df['MASK'].unique()):      
                        trend_sub = trend_df[trend_df["MASK"] == vehicle_wv]      
                          
                        if is_window:    
                            plot_df = (      
                                trend_sub.groupby(COL_TIME)[target_data]      
                                .apply(lambda s: np.percentile(s.dropna(), 10))      
                                .reset_index()      
                            )      
                        else:    
                            plot_df = trend_sub

                        sns.scatterplot(      
                            x=COL_TIME,      
                            y=target_data,      
                            data=plot_df,      
                            ax=ax,      
                            color=[      
                                "grey",      
                                "orange",      
                                "skyblue",      
                                "coral",      
                                "cyan",      
                                "pink",      
                                "salmon",      
                            ][i],      
                            label=vehicle_wv,      
                            edgecolor="black",      
                            linewidth=0.3,      
                        )

                trend_target = df_target[df_target["match_key"] == match_key] if "match_key" in df_target.columns else df_target

                if is_window:    
                    plot_df = (      
                        trend_target.groupby(COL_TIME)[target_data]      
                        .apply(lambda s: np.percentile(s.dropna(), 10))      
                        .reset_index()      
                    )      
  
              else:    
                    plot_df = trend_target

                sns.scatterplot(      
                    x=COL_TIME,      
                    y=target_data,      
                    data=plot_df,      
                    ax=ax,      
                    color="red",      
                    label=f"{match_key}",      
                    edgecolor="black",      
                    s=75
                    )

                if not np.isnan(spec_low):      
                    ax.axhline(spec_low, color="red", linestyle="--", lw=2.5)      
                if not np.isnan(spec_high):      
                    ax.axhline(spec_high, color="red", linestyle="--", lw=2.5)  

                if log_scale:      
                    ax.set_yscale("log")  

                ax.set_title(      
                    f"{target_data_changed} Trend [P10]"      
                    if is_window      
                    else f"{target_data_changed} Trend [Site]",      
                    fontsize=18,      
                )  

                ax.set_xlabel("DC_TKOUTTIME", fontsize=14)      
                ax.set_ylabel("")      
                ax.tick_params(axis="x", labelsize=10)      
                ax.tick_params(axis="y", labelsize=13)      
                ax.grid(True)      
                ax.legend(loc="upper left", fontsize=13)      
                plt.tight_layout()

                img_buf = BytesIO()      
                fig.savefig(img_buf, format="JPEG", bbox_inches="tight")      
                img_buf.seek(0)      
                with Image.open(img_buf) as img:      
                    final_stream = BytesIO()      
                    img.save(final_stream, format="JPEG", quality=img_quality)      
                    final_stream.seek(0)      
                plt.close()

                slide.shapes.add_picture(      
                    final_stream,      
                    left=slide_width_tick * 17,      
                    top=margin,      
                    width=slide_width_tick * 7,      
                    height=slide_width_tick * 4 - margin,      
                )

                agg_funcs = (  
                    agg_funcs_dict_upper  
                    if alias in spec_dict.index and str(spec_dict.loc[alias, COL_DIRECTION]).upper() == "UPPER"  
                    else agg_funcs_dict_lower  
                    if alias in spec_dict.index and str(spec_dict.loc[alias, COL_DIRECTION]).upper() == "LOWER"  
                    else agg_funcs_dict_both  
                )    
                agg_df_group = df_group.groupby("WAFER_ID_str")[target_data].agg(    
                    **agg_funcs    
                ).T    
                agg_df_group = agg_df_group.reindex(columns=x_list)

                if "Window" in alias:    
                    tmp = agg_df_group.loc[["P10"]]    
                else:    
                    tmp = agg_df_group.loc[["MED"]]    
                tmp.index = f"{target_data_changed}_" + tmp.index.astype(str)

                num_rows = agg_df_group.shape[0] + 2    
                num_cols = agg_df_group.shape[1] + 1    
                table_width = int(middle_of_slide - margin)    
                table_shape = slide.shapes.add_table(  
                    num_rows,  
                    num_cols,  
                    margin,  
                    title_space,  
                    table_width,  
                    1,  
                )  
                table_shape.top -= Pt(7)  
                table = table_shape.table

                header_txt = f"[{target_lot_id}, {group_name_str}] {target_data_changed}[{target_unit}] Statistical Table"

                for r in range(num_rows):  
                    for c in range(num_cols):  
                        cell = table.cell(r, c)  
                        tf = cell.text_frame  
                        tf.word_wrap = False  
                        tf.auto_size = None  
                        tf.margin_left = 0  
                        tf.margin_right = 0  
                        tf.margin_top = 0  
                        tf.margin_bottom = 0

                        for para in tf.paragraphs:  
                            para.alignment = PP_ALIGN.CENTER  
                            para.font.name = "Arial"  
                            para.font.size = Pt(7.5)  
                            para.font.bold = False

                for col_idx, col_name in enumerate(agg_df_group, start=1):  
                    for row_idx in (0, 1):  
                        cell = table.cell(row_idx, col_idx)

                        tf = cell.text_frame  
                        tf.clear()  
                        tf.word_wrap = False  
                        tf.auto_size = None  
                        tf.margin_left = 0  
                        tf.margin_right = 0  
                        tf.margin_top = 0  
                        tf.margin_bottom = 0

                        para = tf.paragraphs[0]  
                        para.alignment = PP_ALIGN.CENTER  
                        para.font.name = "Arial"  
                        para.font.size = Pt(7.5)  
                        para.font.bold = False

                        run = para.add_run()  
                        run.font.name = "Arial"  
                        run.font.bold = True

                        if row_idx == 1:  
                            clean_name = str(col_name).replace("\n", "").replace("\r", "").strip()  
                            run.text = f"#{clean_name}"  
                            run.font.size = Pt(9)  
                        else:  
                            run.text = header_txt if col_idx == 1 else ""  
                            run.font.size = Pt(14)

                for r, idx_name in enumerate(agg_df_group.index, start=2):    
                    cell = table.cell(r, 0)

                    tf = cell.text_frame  
                    tf.clear()  
                    tf.word_wrap = False  
                    tf.auto_size = None  
                    tf.margin_left = 0  
                    tf.margin_right = 0  
                    tf.margin_top = 0  
                    tf.margin_bottom = 0

                    para = tf.paragraphs[0]  
                    para.alignment = PP_ALIGN.CENTER  
                    para.font.name = "Arial"  
                    para.font.size = Pt(7.5)  
                    para.font.bold = False

                    run = para.add_run()  
                    run.text = str(idx_name)  
                    run.font.name = "Arial"  
                    run.font.size = Pt(7.5)

                for r, row_vals in enumerate(agg_df_group.values, start=2):    
                    for c, val in enumerate(row_vals, start=1):    
                        cell = table.cell(r, c)

                        tf = cell.text_frame  
                        tf.clear()  
                        tf.word_wrap = False  
                        tf.auto_size = None  
                        tf.margin_left = 0  
                        tf.margin_right = 0  
                        tf.margin_top = 0  
                        tf.margin_bottom = 0

                        para = tf.paragraphs[0]  
                        para.alignment = PP_ALIGN.CENTER  
                        para.font.name = "Arial"  
                        para.font.size = Pt(7.5)  
                        para.font.bold = False

                        run = para.add_run()  
                        run.font.name = "Arial"  
                        run.font.size = Pt(7.5)

                        if pd.isna(val):    
                            txt = ""    
                            run.font.color.rgb = RGBColor(0, 0, 0)    
                        else:    
                            try:    
                                v = float(val)

                                if (not np.isnan(spec_high) and v > spec_high) or (not np.isnan(spec_low) and v < spec_low):    
                                    run.font.color.rgb = RGBColor(255, 0, 0)    
                                else:    
                                    run.font.color.rgb = RGBColor(0, 0, 0)

                                txt = f"{v:.3g}" if 0.01 <= abs(v) < 10000 else f"{v:.2e}"    
                            except Exception:    
                                txt = ""    
                                run.font.color.rgb = RGBColor(0, 0, 0)

                        run.text = txt

                table.rows[0].cells[1].merge(table.rows[0].cells[num_cols - 1])

                fig, ax = plt.subplots(figsize=(1, 2))    
                legend_elems = [    
                    Patch(facecolor=col, label=lab) for lab, col in color_dict.items()    
                ]    
                ax.legend(handles=legend_elems, title="WAFER_ID", loc="center")    
                ax.axis("off")    
                plt.tight_layout()    
                img_buf = BytesIO()    
                fig.savefig(img_buf, format="JPEG", bbox_inches="tight")    
                img_buf.seek(0)    
                with Image.open(img_buf) as img:    
                    final_stream = BytesIO()    
                    img.save(final_stream, format="JPEG", quality=img_quality)    
                    final_stream.seek(0)    
                plt.close()    
                slide.shapes.add_picture(    
                    final_stream,    
                    left=(slide_width_tick * 16) - (2 * margin),    
                    top=slide_height_tick,    
                    width=7.5 * margin,    
                    height=55 * margin,    
                )

                fig, ax = plt.subplots(figsize=(9, 6))    
                radius_col = (    
                    "Chip_Radius"    
                    if f"{cat2}_Radius" not in df_group    
                    else f"{cat2}_Radius"    
                )

                for wafer in df_group["WAFER_ID_str"].unique():    
                    grp = df_group[df_group["WAFER_ID_str"] == wafer]    
                    try:    
                        coeff = np.polyfit(    
                            grp[radius_col],    
                            np.log10(np.abs(grp[target_data]) + 1e-15)    
                            if reformatter.loc[alias, "REPORT LOG SCALE"]    
                            else grp[target_data],    
                            3,    
                        )    
                        poly = np.poly1d(coeff)    
                        x_line = np.linspace(grp[radius_col].min(), grp[radius_col].max(), 100)    
                        y_line = (    
                            10 ** poly(x_line)    
                            if reformatter.loc[alias, "REPORT LOG SCALE"]    
                            else poly(x_line)    
                        )    
                        style = (    
                            dict(linewidth=9, linestyle="-")    
                            if wafer == "Ref."    
                            else dict(linewidth=3, linestyle="--", alpha=0.7)    
                        )    
                        ax.plot(x_line, y_line, color=color_dict[wafer], **style)    
                    except Exception:    
                        pass

                for wafer in df_group["WAFER_ID_str"].unique():    
                    if wafer == "Ref.":    
                        continue    
                    sub = df_group[df_group["WAFER_ID_str"] == wafer]    
                    sns.scatterplot(    
                        data=sub,    
                        x=radius_col,    
                        y=target_data,    
                        color=color_dict[wafer],    
                        s=100,    
                        label=wafer,    
                        zorder=3,    
                    )

                if reformatter.loc[alias, "REPORT LOG SCALE"]:    
                    ax.set_yscale("log")    
                ax.set_title("Radius Profile Chart", fontsize=24)    
                ax.set_xlabel(    
                    "Radius[mm]" if radius_col == "Chip_Radius" else "TEG_Radius[mm]",    
                    fontsize=14,    
                )    
                ax.set_ylabel(f"{target_data_changed}[{target_unit}]", fontsize=14)    
                ax.tick_params(axis="x", labelsize=14)    
                ax.tick_params(axis="y", labelsize=14)    
                ax.set_xlim(0, 150)    
                ax.grid(True, alpha=0.5)    
            ax.get_legend().remove()  
                plt.tight_layout()    
                img_buf = BytesIO()    
                fig.savefig(img_buf, format="JPEG", bbox_inches="tight")    
                img_buf.seek(0)    
                with Image.open(img_buf) as img:    
                    final_stream = BytesIO()    
                    img.save(final_stream, format="JPEG", quality=img_quality)    
                    final_stream.seek(0)    
                plt.close()    
                slide.shapes.add_picture(    
                    final_stream,    
                    left=slide_width_tick * 17,    
                    top=slide_height_tick * 4,    
                    width=slide_width_tick * 7,    
                    height=slide_width_tick * 4,    
                )

                fig, ax = plt.subplots(figsize=(9, 4.5))    
                sorted_df = df_group[["WAFER_ID_str", target_data]].dropna().sort_values(    
                    target_data    
                )    
                for wafer_id, sub in sorted_df.groupby("WAFER_ID_str"):    
                    cum = np.arange(1, len(sub) + 1) / len(sub)    
                    marker = "*" if wafer_id == "Ref." else "o"    
                    ax.plot(    
                        sub[target_data],    
                        cum,    
                        marker=marker,    
                        linestyle="-",    
                        color=color_dict[wafer_id],    
                        markersize=20 if wafer_id == "Ref." else 8,    
                        linewidth=10 if wafer_id == "Ref." else 2,    
                        label=wafer_id,    
                    )    
                if reformatter.loc[alias, "REPORT LOG SCALE"]:    
                    ax.set_xscale("log")    
                ax.set_title("Cumulative Distribution Chart", fontsize=24)    
                ax.set_xlabel(f"{target_data_changed}[{target_unit}]", fontsize=14)    
                ax.set_ylabel("Cumulative Probability", fontsize=14)    
                ax.tick_params(axis="both", labelsize=14)    
                ax.grid(True, alpha=0.5)    
                plt.tight_layout()    
                img_buf = BytesIO()    
                fig.savefig(img_buf, format="JPEG", bbox_inches="tight")    
                img_buf.seek(0)    
                with Image.open(img_buf) as img:    
                    final_stream = BytesIO()    
                    img.save(final_stream, format="JPEG", quality=img_quality)    
                    final_stream.seek(0)    
                plt.close()    
                slide.shapes.add_picture(    
                    final_stream,    
                    left=slide_width_tick * 17,    
                    top=slide_height_tick * 8,    
                    width=slide_width_tick * 7,    
                    height=slide_width_tick * 4,    
                )

                df_map = df_group.copy()    
                if reformatter.loc[alias, COL_DIRECTION] == "UPPER":    
                    cmap_seq = np.linspace(tg, spec_high, 10)    
                elif reformatter.loc[alias, COL_DIRECTION] == "LOWER":    
                    cmap_seq = np.linspace(spec_low, tg, 10)    
                else:    
                    cmap_seq = np.linspace(spec_low, spec_high, 10)    
                norm = BoundaryNorm(cmap_seq, cmap.N, clip=True)

                pgm_list = sorted(df_map["PGM(pt)"].unique())    
                row_cnt = len(pgm_list)

                fig, axes = plt.subplots(    
                    row_cnt,    
                    26,    
                    figsize=(1.106 * 26, 1.106 * (row_cnt + 1)),    
                    squeeze=False,  
                )    
                for r in range(row_cnt):    
                    for c in range(26):    
                        ax = axes[r][c]    
                        ax.invert_yaxis()    
                        ax.set_xticks([])    
                        ax.set_yticks([])    
                        ax.set_xlabel("")    
                        ax.set_ylabel("" if c else pgm_list[r].split("(")[0])    
                        ax.axis("equal")    
                        for spine in ax.spines.values():    
                            spine.set_visible(False)

                wf_groups = df_map.groupby(["WAFER_ID", "PGM(pt)"], observed=False)    
                for (wafer_id, pgm), grp in wf_groups:    
                    col_idx = 25 if wafer_id == "Ref." else int(wafer_id) - 1    
                    row_idx = pgm_list.index(pgm)    
                    axes[row_idx][col_idx].scatter(    
                        grp["CHIP_X_ADJ"],    
                        grp["CHIP_Y_ADJ"],    
                        c=grp[target_data],    
                        cmap=cmap,    
                        norm=norm,    
                        marker="s",    
                    )    
                    title = (    
                        f"{wafer_id}"    
                        if wafer_id == "Ref."    
                        else f"{target_lot_id}_{wafer_id}"    
                    )    
                    axes[row_idx][col_idx].set_title(title)

                plt.tight_layout()    
                img_buf = BytesIO()    
                fig.savefig(img_buf, format="JPEG", bbox_inches="tight")    
                img_buf.seek(0)    
                with Image.open(img_buf) as img:    
                    final_stream = BytesIO()    
                    img.save(final_stream, format="JPEG", quality=img_quality)    
                    final_stream.seek(0)    
                plt.close()    
                slide.shapes.add_picture(    
                    final_stream,    
                    margin,    
                    title_space    
                    + 0.25 * (slide_height - title_space)    
                    + 0.5 * slide_height,    
                    middle_of_slide - margin,    
                )

                mappable = ScalarMappable(norm=norm, cmap=cmap)    
                fig, ax = plt.subplots()    
                plt.colorbar(mappable, ax=ax)    
                ax.set_visible(False)    
                plt.tight_layout()    
                img_buf = BytesIO()    
                fig.savefig(img_buf, format="JPEG", bbox_inches="tight")    
                img_buf.seek(0)    
                with Image.open(img_buf) as img:    
                    final_stream = BytesIO()    
                    img.save(final_stream, format="JPEG", quality=img_quality)    
                    final_stream.seek(0)    
                plt.close()    
                slide.shapes.add_picture(    
                    final_stream,    
                    left=(slide_width_tick * 16) - (2 * margin),    
                    top=title_space + slide_height - 25 * margin,    
                    width=5.8 * margin,    
                    height=25 * margin,    
                )

                item_index_table = pd.concat([item_index_table, tmp])

        except Exception as e:    
            print(f"[WARN] {alias} 처리 중 오류 발생:", e)    
            continue

    try:    
        item_index_table.to_csv("final_item_index_table.csv")    
        if not CONFIG.get("ref_turnoff"):    
            item_index_table = item_index_table.drop(    
                columns=["Ref."], errors="ignore"    
            )    
        item_index_table.columns = [int(c) for c in item_index_table.columns]    
        prs = insert_rawdata_board(    
            item_index_table, prs, target_lot_id, "KEYITEM_Method"    
        )    
    except Exception as e:    
        print("[WARN] RAW DATA BOARD 삽입 중 오류:", e)    
        prs = insert_rawdata_board(    
            item_index_table, prs, target_lot_id, "KEYITEM_Method"    
        )

    return prs  

