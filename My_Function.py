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




