"""
services/parser_utils.py - Utility functions, regular expressions, and coordinate clustering for bank statement parsing.
"""
import re
import bisect

# Regex for numeric amount strings
NUMERIC_REGEX = re.compile(
    r'^\s*'
    r'(?:INR|USD|EUR|GBP|Rs\.?|\$)?\s*'    # Optional currency prefix
    r'[-+]?\s*'                            # Optional leading sign
    r'(?:\d{1,3}(?:,\d{3})*|\d+)'          # Digits (with or without commas)
    r'(?:\.\d+)?'                          # Optional decimal part
    r'\s*(?:INR|USD|EUR|GBP|Cr\.?|Dr\.?|[-+])?' # Optional suffix
    r'\s*$',
    re.IGNORECASE
)

# Date Regex including MM/DD, DD-MM-YYYY, DD.MM.YYYY, DD-Jun-YYYY, DD/Jun/YY formats
MONTHS_PAT = r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|january|february|march|april|may|june|july|august|september|october|november|december)'

DATE_REGEX = re.compile(
    r'^('
    r'\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|'       # 12/06/2026, 12-06-26, 12.06.2026
    r'\d{1,2}[./-]' + MONTHS_PAT + r'[./-]\d{2,4}|' # 12-Jun-2026, 12/Jun/26, 12.JUN.2026
    r'\d{4}[./-]\d{1,2}[./-]\d{1,2}|'       # 2026/06/12, 2026.06.12
    r'\d{1,2}\s+' + MONTHS_PAT + r'\s+\d{2,4}|'    # 12 Jun 2026, 12 June 26
    r'' + MONTHS_PAT + r'\s+\d{1,2},?\s+\d{2,4}|'  # June 12, 2026
    r'\d{1,2}\s+' + MONTHS_PAT + r'|'              # 12 Jun, 12 June
    r'' + MONTHS_PAT + r'\s+\d{1,2}|'               # Jun 12, June 12
    r'\d{1,2}[./-]\d{1,2}|'                 # 10/03, 10.03
    r'\d{1,2}[./-]' + MONTHS_PAT + r'|'           # 12-Jun, 12.JUN
    r'' + MONTHS_PAT + r'[./-]\d{1,2}'            # Jun-12, JUN.12
    r')$',
    re.IGNORECASE
)

def is_serial_number(text):
    """Checks if a string is a simple row sequence number like '1.', '2)', '10'."""
    clean = text.strip().rstrip('.').rstrip(')')
    return clean.isdigit() and len(clean) <= 4

def extract_date_prefix(words, get_text_func):
    """
    Greedily extracts a date prefix from a list of word/token objects.
    Supports skipping a leading serial number if followed by a date.
    Cleans OCR artifacts like trailing punctuation, slashes, and noisy year tokens.
    Returns (date_str, num_words_consumed)
    """
    def try_extract(sub_words):
        for n in (3, 2, 1):
            if len(sub_words) >= n:
                joined = " ".join([get_text_func(w) for w in sub_words[:n]])
                cleaned = joined.strip().rstrip(",").rstrip(".").rstrip("|").rstrip("/").strip()
                if DATE_REGEX.match(cleaned):
                    if n == 2 and len(sub_words) >= 3:
                        third = get_text_func(sub_words[2]).strip()
                        third_digits = re.sub(r'\D', '', third.replace('O', '0').replace('o', '0'))
                        if len(third_digits) == 4 or (len(third_digits) == 2 and int(third_digits) < 50):
                            return f"{cleaned} {third_digits}", 3
                    return cleaned, n
        return "", 0

    if len(words) > 1 and is_serial_number(get_text_func(words[0])):
        date_str, consumed = try_extract(words[1:])
        if date_str:
            return date_str, consumed + 1

    return try_extract(words)

# Common Table Header Keywords
HEADER_KEYWORDS = {
    'date', 'txn', 'value', 'particulars', 'description', 'narration', 
    'chq', 'ref', 'withdrawal', 'debit', 'deposit', 'credit', 'balance', 
    'details', 'amount', 'cheque', 'dr', 'cr'
}

def clean_amount(val):
    """
    Cleans raw OCR / parsed text value into a valid float or None.
    Handles currency symbols, trailing minuses ('50.00-'), split decimals, and OCR noise.
    """
    if val is None:
        return None
    val_str = str(val).strip()
    if not val_str:
        return None
    # Strip common OCR noise characters from boundaries
    val_str = re.sub(r'^[|/\\_\[\]{}()]+', '', val_str).strip()
    val_str = re.sub(r'[|/\\_\[\]{}()]+$', '', val_str).strip()
    if not val_str:
        return None
    # Fix OCR split decimals like "245. 64" -> "245.64" before regex match
    val_str = re.sub(r'(\d+)\.\s+(\d+)', r'\1.\2', val_str)
    if not NUMERIC_REGEX.match(val_str):
        return None
    is_neg = val_str.endswith('-') or val_str.startswith('-') or val_str.lower().endswith('dr') or val_str.lower().endswith('dr.')
    # Strip currency prefix and suffix
    val_str = re.sub(r'^(?:INR|USD|EUR|GBP|Rs\.?|\$)\s*', '', val_str, flags=re.IGNORECASE).strip()
    val_str = re.sub(r'\s*(?:INR|USD|EUR|GBP|Cr\.?|Dr\.?)$', '', val_str, flags=re.IGNORECASE).strip()
    # Remove currency symbols and other non-numeric chars except digits and dot
    cleaned = re.sub(r'[^\d\.]', '', val_str.replace(',', ''))
    try:
        num = float(cleaned)
        return -num if is_neg else num
    except ValueError:
        return None

def is_structural_number(text):
    """Checks if a token is a structural transaction number (ref no or amount)."""
    text_clean = text.strip().replace("$", "").replace(",", "")
    if "." in text_clean or "," in text_clean:
        try:
            cleaned = re.sub(r'[^\d\.\-]', '', text_clean)
            float(cleaned)
            return True
        except ValueError:
            return False
    if text_clean.isdigit() and len(text_clean) >= 4:
        return True
    return False

def cluster_coords(coords, tolerance=15):
    """
    Groups numbers that are close to each other within tolerance.
    Returns the average coordinate of each cluster.
    """
    if not coords:
        return []
    sorted_coords = sorted(coords)
    clusters = []
    current_cluster = [sorted_coords[0]]

    for c in sorted_coords[1:]:
        if c - current_cluster[-1] <= tolerance:
            current_cluster.append(c)
        else:
            clusters.append(sum(current_cluster) / len(current_cluster))
            current_cluster = [c]
    if current_cluster:
        clusters.append(sum(current_cluster) / len(current_cluster))
    return clusters

def unpack_crop(page_crop):
    """Unpacks a page crop dictionary or tuple into (rect, cols)."""
    if not page_crop:
        return None, []
    if isinstance(page_crop, dict):
        rect = page_crop.get("rect")
        cols = page_crop.get("cols", [])
        return rect, cols
    elif isinstance(page_crop, (list, tuple)) and len(page_crop) == 4:
        return page_crop, []
    return None, []

def get_page_boxes(crop_rect, page_num):
    """
    Returns a list of dicts: [ { 'rect': (x0, y0, x1, y1), 'cols': [...] }, ... ]
    for the given page_num.
    """
    if not crop_rect:
        return []
    
    val = None
    if isinstance(crop_rect, dict):
        val = crop_rect.get(page_num)
    else:
        val = crop_rect

    if not val:
        return []

    if isinstance(val, list):
        boxes = []
        for item in val:
            if isinstance(item, dict):
                rect = item.get('rect')
                if rect and isinstance(rect, (list, tuple)) and len(rect) == 4:
                    boxes.append({
                        'rect': tuple(rect),
                        'cols': list(item.get('cols', [])),
                        'rows': list(item.get('rows', [])),
                        'origin': item.get('origin', 'user' if item.get('user_modified') else 'auto'),
                        'user_modified': item.get('user_modified', item.get('origin') == 'user')
                    })
            elif isinstance(item, (list, tuple)) and len(item) == 4:
                boxes.append({
                    'rect': tuple(item),
                    'cols': [],
                    'rows': [],
                    'origin': 'auto',
                    'user_modified': False
                })
        boxes.sort(key=lambda b: b['rect'][1])
        return boxes

    if isinstance(val, dict):
        rect = val.get('rect')
        if rect and isinstance(rect, (list, tuple)) and len(rect) == 4:
            return [{
                'rect': tuple(rect),
                'cols': list(val.get('cols', [])),
                'rows': list(val.get('rows', [])),
                'origin': val.get('origin', 'user' if val.get('user_modified') else 'auto'),
                'user_modified': val.get('user_modified', val.get('origin') == 'user')
            }]

    if isinstance(val, (list, tuple)) and len(val) == 4:
        if all(isinstance(x, (int, float)) for x in val):
            return [{
                'rect': tuple(val),
                'cols': [],
                'rows': [],
                'origin': 'auto',
                'user_modified': False
            }]

    return []

def has_active_crops(crop_rect):
    """Checks if crop_rect has any non-empty crop boxes defined."""
    if not crop_rect:
        return False
    if not isinstance(crop_rect, dict):
        boxes = get_page_boxes(crop_rect, 0)
        return len(boxes) > 0
    
    for page_num in crop_rect.keys():
        if len(get_page_boxes(crop_rect, page_num)) > 0:
            return True
    return False

def get_column_index(x0, splitters):
    """Returns 0-based column index based on column splitters."""
    for idx, s in enumerate(splitters):
        if x0 < s:
            return idx
    return len(splitters)

def _nearest_col_index(col_starts, x0):
    """Finds the nearest column index for x0 using binary search."""
    pos = bisect.bisect_left(col_starts, x0)
    if pos == 0:
        return 0
    if pos == len(col_starts):
        return len(col_starts) - 1
    before = col_starts[pos - 1]
    after = col_starts[pos]
    if x0 - before <= after - x0:
        return pos - 1
    return pos

def _find_first_user_cols(crop_rect):
    """Finds the first user-defined cols from any crop box in crop_rect."""
    if not crop_rect or not isinstance(crop_rect, dict):
        return None
    for page_num in sorted(crop_rect.keys()):
        boxes = get_page_boxes(crop_rect, page_num)
        for box in boxes:
            if box.get("cols"):
                return box.get("cols")
    return None
