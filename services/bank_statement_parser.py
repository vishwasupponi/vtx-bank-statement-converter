"""
services/bank_statement_parser.py - Core Bank Statement Parser Engine.
Orchestrates PDF text extraction, local EasyOCR, Gemini Vision, and table reconstruction.
"""
import os
import re
import io
import sys
import json
import time
import logging
from PIL import Image

from utils import get_user_data_dir
from services.parser_utils import (
    NUMERIC_REGEX,
    DATE_REGEX,
    HEADER_KEYWORDS,
    clean_amount,
    is_structural_number,
    is_serial_number,
    extract_date_prefix,
    cluster_coords,
    unpack_crop,
    get_page_boxes,
    has_active_crops,
    get_column_index,
    _nearest_col_index,
    _find_first_user_cols
)
from services.auto_detect import auto_detect_page_table

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ParserEngine")

def _get_long_path(p):
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        res = ctypes.windll.kernel32.GetLongPathNameW(str(p), buf, 1024)
        return buf.value if res > 0 else str(p)
    except Exception:
        return str(p)

# Self-resolving vendor path discovery
_CUR_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_CUR_DIR)
_VENDOR_DIR = os.path.join(_MODULE_DIR, "_vendor")

def _ensure_vendor():
    if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
        sys.path.insert(0, _VENDOR_DIR)

_ensure_vendor()

class OCRModuleMissingError(Exception):
    """Raised when local OCR is required for a scanned statement but the OCR module is not installed."""
    pass

def is_ocr_available():
    """Returns True if local EasyOCR neural engine is available and importable."""
    try:
        import easyocr
        return True
    except Exception:
        return False

# Lazy imports for heavy libraries
easyocr = None
genai = None
fitz = None  # PyMuPDF
pdfplumber = None
_cached_ocr_reader = None

def _import_pymupdf():
    global fitz
    if fitz is None:
        import fitz
    return fitz

def _import_pdfplumber():
    global pdfplumber
    if pdfplumber is None:
        import pdfplumber
    return pdfplumber

def _import_easyocr():
    global easyocr
    if easyocr is None:
        try:
            import easyocr
        except (ImportError, ModuleNotFoundError) as e:
            raise OCRModuleMissingError("Install the OCR module from the Addon Store so you can easily extract data from scanned bank statements offline.") from e
    return easyocr

def _import_genai():
    global genai
    if genai is None:
        _ensure_vendor()
        from google import genai
    return genai
    return genai

def _build_transactions_from_boxes(all_boxes_rows, global_K, first_user_cols, col_starts, width_key, auto_organize):
    """
    Direct Cell-by-Cell & Date-anchored row grouping engine.
    - When explicit grid lines (rows/cols) exist: performs exact 2D cell containment.
    - When row lines are missing: automatically groups multiline text using Date anchors in Column 0.
    """
    transactions = []
    for box_data in all_boxes_rows:
        page_num = box_data['page_num']
        page_width = float(box_data[width_key])
        page_height = float(box_data.get('page_height', box_data.get('full_height', 1000.0)))
        box = box_data['box']
        box_rows = box_data['rows']

        rect = box.get('rect', (0.0, 0.0, 1.0, 1.0))
        bx0 = rect[0] * page_width
        by0 = rect[1] * page_height
        bx1 = rect[2] * page_width
        by1 = rect[3] * page_height

        box_cols = box.get('cols')
        if not box_cols and first_user_cols:
            box_cols = first_user_cols

        # Define vertical column boundaries
        if box_cols:
            valid_cols = [c * page_width for c in sorted(box_cols) if rect[0] + 0.01 < c < rect[2] - 0.01]
            x_bounds = [bx0] + valid_cols + [bx1]
            box_K = len(x_bounds) - 1
        else:
            x_bounds = None
            box_K = global_K

        # Collect all individual words with coordinates
        all_box_words = []
        for line_words, _, _ in box_rows:
            for w in line_words:
                w_x0 = w.get("x0", 0.0)
                w_x1 = w.get("x1", w_x0 + 20.0)
                w_x_mid = (w_x0 + w_x1) / 2.0
                w_y_mid = (w.get("top", 0.0) + w.get("bottom", 0.0)) / 2.0 if "top" in w else w.get("y_mid", 0.0)
                all_box_words.append({
                    "text": w.get("text", "").strip(),
                    "x0": w_x0,
                    "x1": w_x1,
                    "x_mid": w_x_mid,
                    "y_mid": w_y_mid
                })

        box_row_splitters = box.get('rows')
        if box_row_splitters:
            valid_row_ys = [r * page_height for r in sorted(box_row_splitters) if rect[1] + 0.005 < r < rect[3] - 0.005]
            y_bounds = [by0] + valid_row_ys + [by1]

            # 2D Grid Cell Containment
            for r_i in range(len(y_bounds) - 1):
                cell_top = y_bounds[r_i]
                cell_bot = y_bounds[r_i + 1]

                cell_words = [w for w in all_box_words if cell_top - 1.0 <= w["y_mid"] < cell_bot + 1.0]
                if not cell_words:
                    continue

                row = [""] * box_K
                for c_i in range(box_K):
                    if x_bounds:
                        c_left = x_bounds[c_i]
                        c_right = x_bounds[c_i + 1]
                        words_in_cell = [
                            w["text"] for w in sorted(cell_words, key=lambda w: (round(w["y_mid"] / 3.0), w["x0"]))
                            if c_left - 1.0 <= w["x_mid"] < c_right + 1.0
                        ]
                    else:
                        words_in_cell = []
                        for w in sorted(cell_words, key=lambda w: (round(w["y_mid"] / 3.0), w["x0"])):
                            if is_structural_number(w["text"]):
                                col_idx = _nearest_col_index(col_starts, w["x0"]) + 1
                            else:
                                col_idx = 1
                            if col_idx == c_i:
                                words_in_cell.append(w["text"])

                    row[c_i] = " ".join(words_in_cell).strip()

                if any(row):
                    transactions.append(row)
            continue

        # Strict Column Splitting when column dividers exist (user-drawn or auto-detected)
        page_splitters = [c * page_width for c in sorted(box_cols)] if box_cols else None
        if page_splitters:
            for line_words, _, _ in box_rows:
                row = [""] * box_K
                for w in line_words:
                    val = w.get("text", "").strip()
                    if not val:
                        continue
                    w_x0 = w.get("x0", 0.0)
                    w_x1 = w.get("x1", w_x0 + 15.0)
                    w_x_mid = (w_x0 + w_x1) / 2.0
                    col_idx = get_column_index(w_x_mid, page_splitters)
                    if col_idx < box_K:
                        row[col_idx] = (row[col_idx] + " " + val).strip()
                    else:
                        row[-1] = (row[-1] + " " + val).strip()
                if any(row):
                    transactions.append(row)
            continue

        # Fallback: Date-anchored row builder when NO column dividers exist
        for line_words, date_str, consumed in box_rows:
            if date_str:
                row = [""] * box_K
                row[0] = date_str
                remaining = line_words[consumed:]

                for w in remaining:
                    val = w.get("text", "").strip()
                    x0 = w.get("x0", 0.0)
                    if is_structural_number(val):
                        col_idx = _nearest_col_index(col_starts, x0) + 1
                    else:
                        col_idx = 1

                    if col_idx < box_K:
                        row[col_idx] = (row[col_idx] + " " + val).strip()
                    else:
                        row[-1] = (row[-1] + " " + val).strip()
                transactions.append(row)
            elif transactions:
                remaining = line_words
                temp_row = [""] * box_K
                has_numbers = False

                for w in remaining:
                    val = w.get("text", "").strip()
                    x0 = w.get("x0", 0.0)
                    if is_structural_number(val):
                        col_idx = _nearest_col_index(col_starts, x0) + 1
                    else:
                        col_idx = 1

                    if col_idx < box_K:
                        temp_row[col_idx] = (temp_row[col_idx] + " " + val).strip()
                    else:
                        temp_row[-1] = (temp_row[-1] + " " + val).strip()

                    if col_idx >= 2 and is_structural_number(val):
                        has_numbers = True

                parent_has_amounts = any(bool(transactions[-1][i]) for i in range(2, box_K)) if transactions else False
                if has_numbers and parent_has_amounts:
                    temp_row[0] = transactions[-1][0]
                    transactions.append(temp_row)
                else:
                    for i in range(1, box_K):
                        if temp_row[i]:
                            transactions[-1][i] = (transactions[-1][i] + " " + temp_row[i]).strip()

    if not transactions and all_boxes_rows:
        for box_data in all_boxes_rows:
            for line_words, _, _ in box_data['rows']:
                row = [""] * global_K
                for w in line_words:
                    val = w.get("text", "").strip()
                    x0 = w.get("x0", 0.0)
                    if is_structural_number(val):
                        col_idx = _nearest_col_index(col_starts, x0) + 1
                    else:
                        col_idx = 0
                    if col_idx < global_K:
                        row[col_idx] = (row[col_idx] + " " + val).strip()
                if any(row):
                    transactions.append(row)

    dict_transactions = []
    for tx in transactions:
        row_dict = {}
        for i in range(len(tx)):
            row_dict[f"col_{i}"] = tx[i]
        dict_transactions.append(row_dict)

    return dict_transactions

def check_is_scanned_pdf(file_path, password=""):
    """Determines if a PDF has selectable vector text or is a scanned image."""
    try:
        doc = _import_pymupdf().open(file_path)
        if doc.is_encrypted and password:
            doc.authenticate(password)
        total_text = ""
        for page in doc:
            total_text += page.get_text()
            if len(total_text.strip()) > 50:
                doc.close()
                return False
        doc.close()
        return len(total_text.strip()) < 50
    except Exception as e:
        logger.error(f"Error checking if PDF is scanned: {e}")
        return True

def parse_txt_delimited(file_path):
    """Parses space-delimited text statement into generic columns."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        word_offsets = []
        parsed_lines = []

        for line in lines:
            tokens = list(re.finditer(r'\S+', line))
            if not tokens:
                continue
            parsed_lines.append(tokens)

            date_str, consumed = extract_date_prefix(tokens, lambda m: m.group())
            if date_str:
                remaining = tokens[consumed:]
                if remaining:
                    word_offsets.append(remaining[0].start())
                    for token in remaining[1:]:
                        if is_structural_number(token.group()):
                            word_offsets.append(token.start())

        col_starts = sorted(cluster_coords(word_offsets, tolerance=6))
        K = len(col_starts) + 1

        transactions = []
        for tokens in parsed_lines:
            date_str, consumed = extract_date_prefix(tokens, lambda m: m.group())

            if date_str:
                row = [""] * K
                row[0] = date_str
                remaining = tokens[consumed:]

                for token in remaining:
                    val = token.group()
                    start = token.start()
                    if is_structural_number(val):
                        col_idx = _nearest_col_index(col_starts, start) + 1
                        row[col_idx] = (row[col_idx] + " " + val).strip()
                    else:
                        row[1] = (row[1] + " " + val).strip()
                transactions.append(row)
            elif transactions:
                remaining = tokens
                row = [""] * K
                has_numbers = False
                temp_row = [""] * K
                for token in remaining:
                    val = token.group()
                    start = token.start()
                    if is_structural_number(val):
                        col_idx = _nearest_col_index(col_starts, start) + 1
                        temp_row[col_idx] = (temp_row[col_idx] + " " + val).strip()
                        if col_idx >= 2:
                            has_numbers = True
                    else:
                        temp_row[1] = (temp_row[1] + " " + val).strip()

                if has_numbers:
                    temp_row[0] = transactions[-1][0]
                    transactions.append(temp_row)
                else:
                    for i in range(1, K):
                        if temp_row[i]:
                            transactions[-1][i] = (transactions[-1][i] + " " + temp_row[i]).strip()

        dict_transactions = []
        for tx in transactions:
            row_dict = {}
            for i in range(K):
                row_dict[f"col_{i}"] = tx[i]
            dict_transactions.append(row_dict)

        return dict_transactions
    except Exception as e:
        logger.error(f"Error parsing space-delimited text statement: {e}")
        return []

def parse_pdf_selectable(file_path, crop_rect=None, auto_organize=True, password="", progress_callback=None):
    """
    Parses digital selectable text PDF into generic columns.
    Supports page-specific crop dictionary with multiple crop boxes per page and password protection.
    """
    try:
        pdf = _import_pdfplumber().open(file_path, password=password if password else None)
        all_boxes_rows = []
        word_x0s = []

        active_crops = has_active_crops(crop_rect)
        first_user_cols = _find_first_user_cols(crop_rect) if active_crops else None

        for page_num, page in enumerate(pdf.pages):
            if progress_callback:
                progress_callback(page_num, len(pdf.pages), f"Parsing page {page_num + 1} of {len(pdf.pages)}...")
            if crop_rect and isinstance(crop_rect, dict) and crop_rect.get(page_num) == 'skip':
                continue

            words = page.extract_words(keep_blank_chars=False)
            if not words:
                continue

            page_boxes = get_page_boxes(crop_rect, page_num) if active_crops else []

            if not page_boxes:
                try:
                    fitz_doc = _import_pymupdf().open(file_path)
                    if fitz_doc.is_encrypted and password:
                        fitz_doc.authenticate(password)
                    detected = auto_detect_page_table(fitz_doc, page_num)
                    fitz_doc.close()
                except Exception:
                    detected = None

                if detected:
                    page_boxes = [detected]
                else:
                    page_boxes = [{
                        'rect': (0.0, 0.0, 1.0, 1.0),
                        'cols': []
                    }]

            for box_idx, box in enumerate(page_boxes):
                rect = box['rect']
                x0_rel, y0_rel, x1_rel, y1_rel = rect

                filtered_words = []
                for w in words:
                    w_x_mid = (w["x0"] + w["x1"]) / (2.0 * page.width)
                    w_y_mid = (w["top"] + w["bottom"]) / (2.0 * page.height)
                    if x0_rel - 0.005 <= w_x_mid <= x1_rel + 0.005 and y0_rel - 0.005 <= w_y_mid <= y1_rel + 0.005:
                        filtered_words.append(w)

                if not filtered_words:
                    continue

                lines_dict = {}
                for w in filtered_words:
                    y_mid = (w["top"] + w["bottom"]) / 2.0
                    matched_line = None
                    for line_y in lines_dict:
                        if abs(line_y - y_mid) < 3.0:
                            matched_line = line_y
                            break
                    if matched_line is None:
                        lines_dict[y_mid] = [w]
                    else:
                        lines_dict[matched_line].append(w)

                sorted_line_keys = sorted(lines_dict.keys())
                box_rows = []
                for y in sorted_line_keys:
                    line_words = sorted(lines_dict[y], key=lambda w: w["x0"])
                    date_str, consumed = extract_date_prefix(line_words, lambda w: w["text"])
                    box_rows.append((line_words, date_str, consumed))

                    if date_str and not first_user_cols:
                        remaining = line_words[consumed:]
                        if remaining:
                            word_x0s.append(remaining[0]["x0"])
                            for w in remaining[1:]:
                                if is_structural_number(w["text"]):
                                    word_x0s.append(w["x0"])

                all_boxes_rows.append({
                    'page_num': page_num,
                    'page_width': page.width,
                    'page_height': page.height,
                    'box_idx': box_idx,
                    'box': box,
                    'rows': box_rows
                })

        if first_user_cols:
            global_K = len(first_user_cols) + 1
            col_starts = []
        else:
            col_starts = sorted(cluster_coords(word_x0s, tolerance=15))
            if not col_starts:
                col_starts = [100.0, 200.0, 300.0, 400.0]
            global_K = len(col_starts) + 1

        dict_transactions = _build_transactions_from_boxes(
            all_boxes_rows, global_K, first_user_cols, col_starts, 'page_width', auto_organize
        )

        pdf.close()
        return dict_transactions
    except Exception as e:
        logger.error(f"Error parsing selectable PDF: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return []

def parse_pdf_local_ocr(file_path, crop_rect=None, auto_organize=True, password="", progress_callback=None):
    """
    Parses scanned PDFs locally using EasyOCR, applying page-specific image cropping from crop_rect dict.
    Supports multiple crop boxes per page and password protected documents.
    """
    try:
        fitz_doc = _import_pymupdf().open(file_path)
        if fitz_doc.is_encrypted and password:
            fitz_doc.authenticate(password)
        global _cached_ocr_reader
        if _cached_ocr_reader is None:
            _cached_ocr_reader = _import_easyocr().Reader(['en'], gpu=False)
        reader = _cached_ocr_reader

        all_boxes_rows = []
        word_x0s = []

        active_crops = has_active_crops(crop_rect)
        first_user_cols = _find_first_user_cols(crop_rect) if active_crops else None

        for page_num in range(len(fitz_doc)):
            if progress_callback:
                progress_callback(page_num, len(fitz_doc), f"OCR processing page {page_num + 1} of {len(fitz_doc)}...")
            if crop_rect and isinstance(crop_rect, dict) and crop_rect.get(page_num) == 'skip':
                continue

            page = fitz_doc[page_num]
            page_boxes = get_page_boxes(crop_rect, page_num) if active_crops else []

            pix = page.get_pixmap(dpi=150)
            img_data = pix.tobytes("png")
            full_img = Image.open(io.BytesIO(img_data))

            if not page_boxes:
                detected = auto_detect_page_table(fitz_doc, page_num)
                if detected:
                    page_boxes = [detected]
                else:
                    page_boxes = [{
                        'rect': (0.0, 0.0, 1.0, 1.0),
                        'cols': []
                    }]

            for box_idx, box in enumerate(page_boxes):
                rect = box['rect']
                x0_rel, y0_rel, x1_rel, y1_rel = rect

                left = int(x0_rel * full_img.width)
                top = int(y0_rel * full_img.height)
                right = int(x1_rel * full_img.width)
                bottom = int(y1_rel * full_img.height)

                cropped_img = full_img.crop((left, top, right, bottom))
                offset_x = left
                offset_y = top

                try:
                    import numpy as np
                    img_np = np.array(cropped_img)
                    results = reader.readtext(img_np)
                except Exception as ocr_err:
                    logger.error(f"OCR read error on page {page_num} box {box_idx}: {ocr_err}")
                    results = []

                if not results:
                    continue

                ocr_words = []
                for b, text, conf in results:
                    x0 = b[0][0] + offset_x
                    y_mid = (b[0][1] + b[2][1]) / 2.0 + offset_y
                    ocr_words.append({"x0": x0, "y_mid": y_mid, "text": text})

                lines_dict = {}
                for w in ocr_words:
                    y_mid = w["y_mid"]
                    matched_line = None
                    for line_y in lines_dict:
                        if abs(line_y - y_mid) < 4.5:
                            matched_line = line_y
                            break
                    if matched_line is None:
                        lines_dict[y_mid] = [w]
                    else:
                        lines_dict[matched_line].append(w)

                sorted_line_keys = sorted(lines_dict.keys())
                box_rows = []
                for y in sorted_line_keys:
                    line_words = sorted(lines_dict[y], key=lambda w: w["x0"])
                    date_str, consumed = extract_date_prefix(line_words, lambda w: w["text"])
                    box_rows.append((line_words, date_str, consumed))

                    if date_str and not first_user_cols:
                        remaining = line_words[consumed:]
                        if remaining:
                            word_x0s.append(remaining[0]["x0"])
                            for w in remaining[1:]:
                                if is_structural_number(w["text"]):
                                    word_x0s.append(w["x0"])

                all_boxes_rows.append({
                    'page_num': page_num,
                    'full_width': full_img.width,
                    'full_height': full_img.height,
                    'box_idx': box_idx,
                    'box': box,
                    'rows': box_rows
                })

        fitz_doc.close()

        if first_user_cols:
            global_K = len(first_user_cols) + 1
            col_starts = []
        else:
            col_starts = sorted(cluster_coords(word_x0s, tolerance=18))
            if not col_starts:
                col_starts = [150.0, 300.0, 450.0, 600.0]
            global_K = len(col_starts) + 1

        dict_transactions = _build_transactions_from_boxes(
            all_boxes_rows, global_K, first_user_cols, col_starts, 'full_width', auto_organize
        )

        for r in dict_transactions:
            for k in list(r.keys()):
                val = r[k]
                if isinstance(val, str):
                    c_val = re.sub(r'^[|/\\_\[\]{}()]+', '', val).strip()
                    c_val = re.sub(r'[|/\\_\[\]{}()]+$', '', c_val).strip()
                    r[k] = c_val

        return dict_transactions
    except OCRModuleMissingError:
        raise
    except Exception as e:
        logger.error(f"Error in local OCR parser: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return []

_cached_ocr_reader = None
_cached_gemini_model = None

def _is_api_key_error(err_str):
    err_lower = str(err_str).lower()
    return (
        "api_key_invalid" in err_lower
        or "api key not valid" in err_lower
        or "api_key_expired" in err_lower
        or "permission_denied" in err_lower
        or "unauthenticated" in err_lower
    )

def _resolve_candidate_gemini_models(client):
    """
    Returns an ordered list of multimodal Gemini models for bank statement parsing:
    1. First Priority: 'gemini-flash-latest' (always points to Google's latest available Flash model,
       keeping the application future-proof without manual code updates).
    2. Dynamic Fallbacks: Dynamically queries client.models.list() for active multimodal models,
       sorting newer Flash models first, followed by other multimodal reasoning models.
    3. Resilient Fallbacks: Standard fallback identifiers in case models.list() fails.
    """
    candidates = ["gemini-flash-latest"]

    try:
        models_list = [
            m.name.replace("models/", "")
            for m in client.models.list()
            if 'generateContent' in (m.supported_actions or [])
        ]
        NON_IMAGE_KEYWORDS = ["tts", "audio", "voice", "embed", "imagen", "robot", "custom", "preview-tts"]
        valid_models = [m for m in models_list if not any(kw in m.lower() for kw in NON_IMAGE_KEYWORDS)]

        # Dynamic sorting: Flash models (newest first), followed by other multimodal models (newest first)
        flash_models = sorted([m for m in valid_models if "flash" in m.lower() and m != "gemini-flash-latest"], reverse=True)
        other_models = sorted([m for m in valid_models if "flash" not in m.lower() and m != "gemini-flash-latest"], reverse=True)

        for m in flash_models + other_models:
            if m not in candidates:
                candidates.append(m)

    except Exception as e:
        if _is_api_key_error(str(e)):
            raise RuntimeError("The entered Gemini API Key is invalid or expired. Please check your API key in the input field or Settings.")
        logger.warning(f"Failed to dynamically query model list: {e}. Using resilient fallbacks.")
        for fallback in ["gemini-3.8-flash", "gemini-4-carbon"]:
            if fallback not in candidates:
                candidates.append(fallback)

    return candidates

def _resolve_working_gemini_model(client):
    """Backward-compatible helper returning the top candidate model."""
    global _cached_gemini_model
    if _cached_gemini_model:
        return _cached_gemini_model
    candidates = _resolve_candidate_gemini_models(client)
    _cached_gemini_model = candidates[0]
    return _cached_gemini_model

GEMINI_PROMPT = (
    "You are a professional financial document parser. Extract all transaction records from this bank statement. "
    "Return the data strictly as a 2D JSON array of arrays (a table grid), where the first column is always the Transaction Date, "
    "and subsequent columns represent Description, Cheque/Ref No, Withdrawal, Deposit, and Balance as present in the statement. "
    "Do NOT return markdown formatting, explanations, or code blocks. Return ONLY the raw JSON array. "
    'Example: [["01/04/2024", "SALARY", "", "", "50000.00", "50000.00"]]'
)

def parse_with_gemini(file_path, api_key, crop_rect=None, password=""):
    """
    Calls Gemini API (google.genai SDK) to extract raw tables from a statement as a generic grid of values.
    Supports page-specific crops from crop_rect dict, including multiple boxes per page.
    """
    try:
        genai_mod = _import_genai()
        genai_types = genai_mod.types
        client = genai_mod.Client(api_key=api_key)

        parts = [
            genai_types.Part.from_text(text=GEMINI_PROMPT),
        ]

        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".txt":
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            parts.append(genai_types.Part.from_text(text=content))
        else:
            logger.info(f"Preparing '{os.path.basename(file_path)}' for AI extraction...")
            fitz_mod = _import_pymupdf()
            doc = fitz_mod.open(file_path)
            if doc.is_encrypted and password:
                doc.authenticate(password)

            active_crops = has_active_crops(crop_rect)

            for page_num in range(len(doc)):
                if crop_rect and isinstance(crop_rect, dict) and crop_rect.get(page_num) == 'skip':
                    continue

                page = doc[page_num]
                page_boxes = get_page_boxes(crop_rect, page_num) if active_crops else []

                logger.info(f"Rendering PDF Page {page_num + 1}/{len(doc)} for AI model...")
                pix = page.get_pixmap(dpi=150)
                img_bytes = pix.tobytes("png")
                full_img = Image.open(io.BytesIO(img_bytes))

                if not active_crops:
                    buf = io.BytesIO()
                    full_img.save(buf, format="PNG")
                    parts.append(genai_types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))
                else:
                    for box in page_boxes:
                        rect = box['rect']
                        x0, y0, x1, y1 = rect
                        cropped = full_img.crop((
                            int(x0 * full_img.width), int(y0 * full_img.height),
                            int(x1 * full_img.width), int(y1 * full_img.height)
                        ))
                        buf = io.BytesIO()
                        cropped.save(buf, format="PNG")
                        parts.append(genai_types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))
            doc.close()

        # Build candidate models: 'gemini-flash-latest' always 1st priority,
        # dynamically followed by the newest available multimodal models discovered from the API.
        unique_models = _resolve_candidate_gemini_models(client)

        response = None
        last_error = None

        for attempt_idx, model_name in enumerate(unique_models):
            logger.info(f"Sending document data to Gemini AI model '{model_name}' (Attempt {attempt_idx + 1}/{len(unique_models)})...")
            logger.info("Awaiting Gemini response (this may take 10-30 seconds depending on server load)...")
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[genai_types.Content(role="user", parts=parts)]
                )
                if response and getattr(response, "text", None):
                    _cached_gemini_model = model_name
                    break
            except Exception as err:
                last_error = err
                err_str = str(err)
                if _is_api_key_error(err_str):
                    raise RuntimeError("The entered Gemini API Key is invalid or expired. Please check your API key in the input field or Settings.")
                elif "503" in err_str or "unavailable" in err_str.lower() or "high demand" in err_str.lower():
                    logger.warning(f"⚡ Google AI model '{model_name}' is experiencing temporary high demand/overload. Retrying on alternate model in 2s...")
                    time.sleep(2)
                elif "deadline" in err_str.lower() or "timeout" in err_str.lower():
                    logger.warning(f"⚡ Google AI request on '{model_name}' timed out. Retrying on alternate model in 2s...")
                    time.sleep(2)
                elif "429" in err_str or "resource_exhausted" in err_str.lower():
                    logger.warning(f"⚡ Rate limit reached on '{model_name}'. Retrying on alternate model in 2s...")
                    time.sleep(2)
                elif "404" in err_str or "not found" in err_str.lower():
                    logger.warning(f"⚡ Model '{model_name}' not available on current API tier. Trying next model...")
                else:
                    logger.warning(f"⚡ Notice from '{model_name}': {err}. Trying next model...")

        if response is None or not getattr(response, "text", None):
            err_msg = str(last_error) if last_error else "Unknown error"
            if _is_api_key_error(err_msg):
                clean_msg = "The entered Gemini API Key is invalid or expired. Please check your API key in the input field or Settings."
            elif "503" in err_msg or "unavailable" in err_msg.lower() or "high demand" in err_msg.lower():
                clean_msg = "Google AI servers are currently experiencing temporary high demand/overload. Please try again in a few moments, or select Local OCR extraction."
            elif "deadline" in err_msg.lower() or "timeout" in err_msg.lower():
                clean_msg = "Google AI request timed out due to temporary high server traffic. Please try again in a few moments."
            elif "429" in err_msg or "resource_exhausted" in err_msg.lower():
                clean_msg = "Google AI rate limit reached for your API key. Please wait a minute before retrying, or use Local OCR extraction."
            else:
                clean_msg = f"Gemini Cloud AI extraction could not be completed: {err_msg}"
            raise RuntimeError(clean_msg)

        logger.info("AI response received! Parsing extracted transaction table...")
        response_text = response.text.strip()
        if response_text.startswith("```"):
            response_text = re.sub(r'^```(?:json)?\n', '', response_text)
            response_text = re.sub(r'\n```$', '', response_text)

        grid = json.loads(response_text)

        if grid and len(grid) > 0:
            first_row = grid[0]
            is_header = True
            for cell in first_row:
                if DATE_REGEX.match(str(cell).strip()) or clean_amount(cell) is not None:
                    is_header = False
                    break
            if is_header:
                grid = grid[1:]

        transactions = []
        if grid:
            K = max(len(row) for row in grid)
            for row in grid:
                row_dict = {}
                for i in range(K):
                    val = row[i] if i < len(row) else ""
                    row_dict[f"col_{i}"] = str(val).strip()
                transactions.append(row_dict)
        return transactions

    except Exception as e:
        logger.error(f"Error in Gemini parser: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise  # Re-raise so the UI can show the actual error to the user

def process_file(file_path, use_ocr=False, use_gemini=False, api_key="", crop_rect=None, auto_organize=True, password="", progress_callback=None):
    """
    Main entry point. Processes a file and returns generic column transactions.
    Supports password-protected PDFs.
    """
    logger.info(f"Processing file: {file_path} (use_ocr={use_ocr}, use_gemini={use_gemini}, crop_rect={crop_rect}, auto_organize={auto_organize})")

    if not os.path.exists(file_path):
        return []

    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".txt":
        if use_gemini and api_key:
            transactions = parse_with_gemini(file_path, api_key, crop_rect, password=password)
        else:
            transactions = parse_txt_delimited(file_path)
    elif ext == ".pdf":
        if use_gemini and api_key:
            transactions = parse_with_gemini(file_path, api_key, crop_rect, password=password)
        else:
            is_scanned = check_is_scanned_pdf(file_path, password=password)
            if is_scanned:
                if not is_ocr_available():
                    raise OCRModuleMissingError("Install the OCR module from the Addon Store so you can easily extract data from scanned bank statements offline.")
                transactions = parse_pdf_local_ocr(file_path, crop_rect, auto_organize=auto_organize, password=password, progress_callback=progress_callback)
            else:
                transactions = parse_pdf_selectable(file_path, crop_rect, auto_organize=auto_organize, password=password, progress_callback=progress_callback)
                if not transactions and use_ocr:
                    if not is_ocr_available():
                        raise OCRModuleMissingError("Install the OCR module from the Addon Store so you can easily extract data from scanned bank statements offline.")
                    transactions = parse_pdf_local_ocr(file_path, crop_rect, auto_organize=auto_organize, password=password, progress_callback=progress_callback)
    else:
        logger.warning(f"Unsupported file format: {ext}")
        transactions = []

    # Skip post-processing when Gemini AI was used — AI output is already clean.
    # Only apply auto_organize cleanup for local parsing methods.
    if not use_gemini:
        if auto_organize:
            for tx in transactions:
                for k, v in tx.items():
                    if k != "Source":
                        cleaned_num = clean_amount(v)
                        if cleaned_num is not None and ("." in str(v) or "," in str(v) or str(v).isdigit()):
                            if len(str(v).strip()) <= 4 and str(v).strip().isdigit():
                                tx[k] = str(v).strip()
                            else:
                                tx[k] = cleaned_num
                        else:
                            tx[k] = str(v).strip()
        else:
            for tx in transactions:
                for k, v in tx.items():
                    if k != "Source":
                        tx[k] = str(v).strip()

    return transactions
