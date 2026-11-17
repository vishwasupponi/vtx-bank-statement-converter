"""
services/auto_detect.py - Table Auto-Detection Engine for Bank Statements.
Includes Native Vector (digital), Multi-Column Vertical Profile (scanned), and Layout Fallbacks.
"""
import re
import logging
import numpy as np
from services.parser_utils import cluster_coords, DATE_REGEX, HEADER_KEYWORDS

logger = logging.getLogger("AutoDetectEngine")

def auto_detect_page_table(doc, page_num):
    """
    Differentiated 2-Track Robust Auto-Detection Engine:
      Track A: Digital Selectable Text PDFs (Native vector table analysis with header inference)
      Track B: Scanned Image PDFs (Deskew correction, multi-column vertical profile segmentation, and zebra row clustering)
    Returns {'rect': (x0, y0, x1, y1), 'cols': [...], 'rows': [...]} or None.
    """
    try:
        page = doc[page_num]
        page_w = page.rect.width
        page_h = page.rect.height

        raw_words = page.get_text("words")
        is_scanned = len(raw_words) < 10

        # =============================================================
        # TRACK A: Digital Selectable Text PDF - Native Vector Analysis
        # =============================================================
        if not is_scanned:
            # 1. Check native PDF table vector drawings
            try:
                tabs = page.find_tables()
                if tabs and tabs.tables:
                    t = tabs.tables[0]
                    bx0, by0, bx1, by1 = t.bbox
                    if (bx1 - bx0) > page_w * 0.4 and (by1 - by0) > page_h * 0.15:
                        v_cols = [round(c / page_w, 4) for c in t.cols if bx0 + 5 < c < bx1 - 5]
                        # Dedup micro-rows (< 12pt)
                        raw_rows = sorted([r for r in t.rows if by0 + 5 < r < by1 - 5])
                        filtered_rows = []
                        for r in raw_rows:
                            if not filtered_rows or (r - filtered_rows[-1]) > 12:
                                filtered_rows.append(r)
                            else:
                                filtered_rows[-1] = r
                        # Dedup micro-cols (< 8pt)
                        raw_cols = sorted([c for c in t.cols if bx0 + 5 < c < bx1 - 5])
                        filtered_cols = []
                        for c in raw_cols:
                            if not filtered_cols or (c - filtered_cols[-1]) > 8:
                                filtered_cols.append(c)
                            else:
                                filtered_cols[-1] = c
                        v_cols = [round(c / page_w, 4) for c in filtered_cols]
                        h_rows = [round(r / page_h, 4) for r in filtered_rows]
                        return {
                            'rect': (round(bx0 / page_w, 4), round(by0 / page_h, 4), round(bx1 / page_w, 4), round(by1 / page_h, 4)),
                            'cols': v_cols,
                            'rows': h_rows
                        }
            except Exception:
                pass

            # 2. Find header either on current page or search prior pages
            def _get_page_lines(p_idx):
                p = doc[p_idx]
                words = p.get_text("words")
                lines_by_y = {}
                for w in words:
                    x0, y0, x1, y1, text = w[:5]
                    y_mid = (y0 + y1) / 2.0
                    matched = None
                    for ly in lines_by_y:
                        if abs(ly - y_mid) < 3.5:
                            matched = ly
                            break
                    if matched is None:
                        lines_by_y[y_mid] = [w]
                    else:
                        lines_by_y[matched].append(w)
                return lines_by_y, p.rect.width, p.rect.height

            SUMMARY_KWS = ['statement summary', 'summary :', 'opening balance', 'dr count', 'cr count', 'total :', 'generated on:', 'page :']

            current_lines, cur_w, cur_h = _get_page_lines(page_num)
            best_header_y = None
            best_header_words = None
            max_kws = 0

            for y in sorted(current_lines.keys()):
                lw = sorted(current_lines[y], key=lambda x: x[0])
                line_text = " ".join(w[4].lower() for w in lw)
                if any(s in line_text for s in SUMMARY_KWS):
                    continue
                kw_count = sum(1 for kw in HEADER_KEYWORDS if re.search(r'\b' + kw + r'\b', line_text))
                if kw_count >= 3 and kw_count > max_kws:
                    max_kws = kw_count
                    best_header_y = y
                    best_header_words = lw

            cols = []
            bx0, by0, bx1 = 0.05, 0.25, 0.98

            if best_header_words and max_kws >= 3:
                bx0 = round(max(0.0, best_header_words[0][0] / cur_w - 0.01), 4)
                bx1 = round(min(1.0, best_header_words[-1][2] / cur_w + 0.01), 4)
                by0 = round(max(0.0, best_header_words[0][1] / cur_h - 0.005), 4)

                # Find transaction date bounds below header
                p_curr = doc[page_num]
                curr_words = p_curr.get_text("words")
                txn_date_x1s = []
                for w in curr_words:
                    if w[1] > best_header_y and DATE_REGEX.match(w[4].strip()) and w[0] < cur_w * 0.25:
                        txn_date_x1s.append(w[2] / cur_w)
                date_end_x = round(max(txn_date_x1s) + 0.008, 4) if txn_date_x1s else round(best_header_words[0][2] / cur_w + 0.015, 4)

                # Merge compound headers like 'Withdrawal Amt.', 'Deposit Amt.', 'Closing Balance'
                header_blocks = []
                for w in best_header_words:
                    if not header_blocks:
                        header_blocks.append([w])
                    else:
                        last_w = header_blocks[-1][-1]
                        gap = (w[0] - last_w[2]) / cur_w
                        if gap < 0.010:
                            header_blocks[-1].append(w)
                        else:
                            header_blocks.append([w])

                for i in range(len(header_blocks) - 1):
                    curr_block = header_blocks[i]
                    next_block = header_blocks[i + 1]
                    curr_text = " ".join(w[4].lower() for w in curr_block)
                    
                    if i == 0 and "date" in curr_text:
                        cols.append(date_end_x)
                    else:
                        next_x0 = next_block[0][0] / cur_w
                        cols.append(round(next_x0 - 0.008, 4))
            else:
                for prev_p in range(page_num - 1, -1, -1):
                    prev_lines, pw, ph = _get_page_lines(prev_p)
                    for py in sorted(prev_lines.keys()):
                        plw = sorted(prev_lines[py], key=lambda x: x[0])
                        pline_text = " ".join(w[4].lower() for w in plw)
                        if any(s in pline_text for s in SUMMARY_KWS):
                            continue
                        p_kws = sum(1 for kw in HEADER_KEYWORDS if re.search(r'\b' + kw + r'\b', pline_text))
                        if p_kws >= 3:
                            bx0 = round(max(0.0, plw[0][0] / pw - 0.01), 4)
                            bx1 = round(min(1.0, plw[-1][2] / pw + 0.01), 4)
                            # Find transaction date bounds on prev page
                            p_prev_doc = doc[prev_p]
                            p_words = p_prev_doc.get_text("words")
                            p_date_x1s = []
                            for w in p_words:
                                if w[1] > py and DATE_REGEX.match(w[4].strip()) and w[0] < pw * 0.25:
                                    p_date_x1s.append(w[2] / pw)
                            p_date_end_x = round(max(p_date_x1s) + 0.008, 4) if p_date_x1s else round(plw[0][2] / pw + 0.015, 4)

                            h_blocks = []
                            for w in plw:
                                if not h_blocks:
                                    h_blocks.append([w])
                                else:
                                    gap = (w[0] - h_blocks[-1][-1][2]) / pw
                                    if gap < 0.010:
                                        h_blocks[-1].append(w)
                                    else:
                                        h_blocks.append([w])

                            for i in range(len(h_blocks) - 1):
                                curr_block = h_blocks[i]
                                next_block = h_blocks[i + 1]
                                curr_text = " ".join(w[4].lower() for w in curr_block)
                                if i == 0 and "date" in curr_text:
                                    cols.append(p_date_end_x)
                                else:
                                    next_x0 = next_block[0][0] / pw
                                    cols.append(round(next_x0 - 0.008, 4))
                            break
                    if cols:
                        break

            # Find dates and summary on current page
            date_ys = []
            last_txn_y1 = None
            summary_y0 = None

            search_y_start = (best_header_y + 8) if best_header_y else (0.15 * cur_h)
            sorted_ys = [y for y in sorted(current_lines.keys()) if y > search_y_start]

            tx_blocks = []
            for y in sorted_ys:
                lw = sorted(current_lines[y], key=lambda x: x[0])
                line_text = " ".join(w[4].lower() for w in lw)

                if any(fw in line_text for fw in SUMMARY_KWS):
                    if summary_y0 is None:
                        summary_y0 = lw[0][1]
                    break

                if lw and DATE_REGEX.match(lw[0][4].strip()) and lw[0][0] < cur_w * 0.28:
                    tx_blocks.append({'date_y': y, 'lines': [lw]})
                    date_ys.append(y / cur_h)
                    last_txn_y1 = max(last_txn_y1 or 0, max(w[3] for w in lw))
                elif tx_blocks:
                    tx_blocks[-1]['lines'].append(lw)
                    last_txn_y1 = max(last_txn_y1 or 0, max(w[3] for w in lw))

            h_rows = []
            for i in range(len(tx_blocks) - 1):
                curr_tx = tx_blocks[i]
                next_tx = tx_blocks[i + 1]
                curr_bottom = max(max(w[3] for w in l) for l in curr_tx['lines'])
                next_top = min(w[1] for w in next_tx['lines'][0])
                split_y = (curr_bottom + next_top) / (2.0 * cur_h)
                h_rows.append(round(split_y, 4))

            if best_header_words and tx_blocks:
                h_bot = max(w[3] for w in best_header_words)
                f_top = min(w[1] for w in tx_blocks[0]['lines'][0])
                by0 = round(max(0.0, (h_bot + f_top) / (2.0 * cur_h)), 4)
            elif not best_header_words and date_ys:
                by0 = round(max(0.0, date_ys[0] - 0.02), 4)

            if summary_y0 is not None:
                by1 = round(min(1.0, (summary_y0 - 2.0) / cur_h), 4)
            elif last_txn_y1 is not None:
                by1 = round(min(1.0, (last_txn_y1 + 4.0) / cur_h), 4)
            else:
                by1 = 0.95

            return {
                'rect': (bx0, by0, bx1, by1),
                'cols': cols,
                'rows': h_rows
            }

        # =============================================================
        # TRACK B: Scanned Image & Image-Based Line Analysis
        # =============================================================
        try:
            import cv2
            pix = page.get_pixmap(dpi=150)
            img_rgb = np.frombuffer(pix.tobytes('png'), np.uint8)
            img_rgb = cv2.imdecode(img_rgb, cv2.IMREAD_COLOR)
            img_gray = cv2.cvtColor(img_rgb, cv2.COLOR_BGR2GRAY)
            ih, iw = img_gray.shape

            # 1. Automatic Deskew Angle Correction
            edges = cv2.Canny(img_gray, 50, 150, apertureSize=3)
            h_lines_p = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=70, minLineLength=iw * 0.15, maxLineGap=20)
            angles = []
            if h_lines_p is not None:
                for line in h_lines_p:
                    x1, y1, x2, y2 = line.ravel()[:4]
                    dx = x2 - x1

                    dy = y2 - y1
                    if dx != 0:
                        a = np.degrees(np.arctan2(dy, dx))
                        if abs(a) < 12.0:
                            angles.append(a)
                        elif abs(a) > 78.0:
                            va = a - 90.0 if a > 0 else a + 90.0
                            if abs(va) < 12.0:
                                angles.append(va)

            skew_angle = float(np.median(angles)) if angles else 0.0
            if abs(skew_angle) > 0.2:
                center = (iw // 2, ih // 2)
                M = cv2.getRotationMatrix2D(center, skew_angle, 1.0)
                img_gray = cv2.warpAffine(img_gray, M, (iw, ih), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=255)
                img_rgb = cv2.warpAffine(img_rgb, M, (iw, ih), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))

            _, bin_inv = cv2.threshold(img_gray, 180, 255, cv2.THRESH_BINARY_INV)

            # 2. Extract Vertical and Horizontal Line Features
            v_len = max(15, int(ih * 0.03))
            v_lines_img = cv2.morphologyEx(bin_inv, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_len)))
            cnts_v, _ = cv2.findContours(v_lines_img, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            h_len = max(15, int(iw * 0.10))
            h_lines_img = cv2.morphologyEx(bin_inv, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (h_len, 1)))
            cnts_h, _ = cv2.findContours(h_lines_img, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            # -------------------------------------------------------------
            # STRATEGY 1: Multi-Column Vertical Profile (IndusInd, Scanned Grid)
            # -------------------------------------------------------------
            v_segments = []
            for c in cnts_v:
                x, y, w, h = cv2.boundingRect(c)
                if h >= 25 and w <= 35:
                    v_segments.append((x + w / 2.0, y, y + h))

            if v_segments:
                x_clusters = []
                for x, y0, y1 in sorted(v_segments, key=lambda s: s[0]):
                    matched = False
                    for cl in x_clusters:
                        if abs(cl['x'] - x) < 20:
                            cl['segments'].append((y0, y1))
                            cl['x'] = (cl['x'] + x) / 2.0
                            matched = True
                            break
                    if not matched:
                        x_clusters.append({'x': x, 'segments': [(y0, y1)]})

                if len(x_clusters) >= 2:
                    y_profile = np.zeros(ih, dtype=int)
                    for y in range(ih):
                        count = 0
                        for cl in x_clusters:
                            for y0, y1 in cl['segments']:
                                if y0 <= y <= y1:
                                    count += 1
                                    break
                        y_profile[y] = count

                    is_table_y = (y_profile >= 2).astype(np.int8)
                    diffs = np.diff(np.pad(is_table_y, (1, 1), 'constant'))
                    starts = np.where(diffs == 1)[0]
                    ends = np.where(diffs == -1)[0]

                    candidates = []
                    for s, e in zip(starts, ends):
                        span = (e - s) / float(ih)
                        if span >= 0.02 and s > ih * 0.03:
                            active_x = []
                            for cl in x_clusters:
                                if any(max(s, y0) < min(e, y1) for y0, y1 in cl['segments']):
                                    active_x.append(cl['x'] / float(iw))
                            if len(active_x) >= 2:
                                candidates.append({
                                    's': s, 'e': e, 'span': span,
                                    'cols_count': len(active_x),
                                    'active_x': sorted(active_x)
                                })

                    if candidates:
                        best = max(candidates, key=lambda c: (c['cols_count'], c['span']))
                        by0 = round(best['s'] / float(ih), 4)
                        by1 = round(best['e'] / float(ih), 4)
                        active_x = sorted(best['active_x'])
                        bx0 = round(max(0.02, active_x[0]), 4)
                        bx1 = round(min(0.98, active_x[-1]), 4)
                        v_cols = [round(x, 4) for x in active_x if bx0 + 0.02 < x < bx1 - 0.02]

                        by0_px = max(0, int(by0 * ih))
                        by1_px = min(ih, int(by1 * ih))
                        bx0_px = max(0, int(bx0 * iw))
                        bx1_px = min(iw, int(bx1 * iw))

                        in_range_h = []
                        for c in cnts_h:
                            x, y, w, h = cv2.boundingRect(c)
                            y_rel = (y + h / 2.0) / float(ih)
                            if (by0 - 0.01) <= y_rel <= (by1 + 0.01) and w > iw * 0.20:
                                in_range_h.append(y_rel)

                        h_rows = []
                        if in_range_h:
                            clust_h = sorted(cluster_coords(in_range_h, tolerance=0.015))
                            h_rows = [round(y, 4) for y in clust_h if by0 + 0.015 < y < by1 - 0.015]

                        if len(h_rows) < 2:
                            try:
                                roi_gray = img_gray[by0_px:by1_px, bx0_px:bx1_px]
                                if roi_gray.shape[0] > 20 and roi_gray.shape[1] > 50:
                                    row_means = np.mean(roi_gray, axis=1).astype(float)
                                    kern = np.ones(9) / 9.0
                                    row_smooth = np.convolve(row_means, kern, mode='same')
                                    bright_ref = np.percentile(row_smooth, 85)
                                    dark_ref   = np.percentile(row_smooth, 15)
                                    if (bright_ref - dark_ref) > 8:
                                        mid_thresh = (bright_ref + dark_ref) / 2.0
                                        is_bright = (row_smooth > mid_thresh).astype(np.int8)
                                        transitions = np.where(np.diff(is_bright) != 0)[0]
                                        if len(transitions) >= 2:
                                            raw_t = [by0 + int(t) / float(ih) for t in transitions if 8 < t < (by1_px - by0_px) - 8]
                                            clust_t = sorted(cluster_coords(raw_t, tolerance=0.025))
                                            h_rows = [round(y, 4) for y in clust_t if by0 + 0.015 < y < by1 - 0.015]
                            except Exception:
                                pass

                        return {
                            'rect': (bx0, by0, bx1, by1),
                            'cols': v_cols,
                            'rows': h_rows
                        }

            # -------------------------------------------------------------
            # STRATEGY 2: Horizontal Lines Block (SBI Scanned, Rule Tables)
            # -------------------------------------------------------------
            table_h_lines = []
            for c in cnts_h:
                x, y, w, h = cv2.boundingRect(c)
                if w > iw * 0.25:
                    table_h_lines.append({
                        'y': (y + h / 2.0) / float(ih),
                        'x0': x / float(iw),
                        'x1': (x + w) / float(iw),
                        'w': w
                    })

            if table_h_lines:
                table_h_lines = sorted(table_h_lines, key=lambda l: l['y'])
                blocks = [[table_h_lines[0]]]
                for hl in table_h_lines[1:]:
                    if hl['y'] - blocks[-1][-1]['y'] < 0.20:
                        blocks[-1].append(hl)
                    else:
                        blocks.append([hl])

                best_block = max(blocks, key=lambda b: (b[-1]['y'] - b[0]['y']) if len(b) > 1 else b[0]['w'])
                blk_span = (best_block[-1]['y'] - best_block[0]['y']) if len(best_block) > 1 else 0
                blk_top = best_block[0]['y']
                blk_bot = best_block[-1]['y']

                is_header_footer = (blk_span < 0.04 and len(best_block) <= 2 and (blk_top < 0.08 or blk_bot > 0.94))

                if not is_header_footer:
                    by0 = round(blk_top, 4)
                    by1 = round(blk_bot, 4) if len(best_block) > 1 else round(blk_top + 0.08, 4)
                    bx0 = round(min(hl['x0'] for hl in best_block), 4)
                    bx1 = round(max(hl['x1'] for hl in best_block), 4)

                    v_in_block = []
                    for c in cnts_v:
                        x, y, w, h = cv2.boundingRect(c)
                        y_rel = (y + h / 2.0) / float(ih)
                        x_rel = (x + w / 2.0) / float(iw)
                        if (by0 - 0.02) <= y_rel <= (by1 + 0.02) and (bx0 - 0.02) <= x_rel <= (bx1 + 0.02):
                            if h > 12 and w < 18:
                                v_in_block.append(x_rel)

                    clustered_v = sorted(cluster_coords(v_in_block, tolerance=0.015)) if v_in_block else []
                    v_cols = [round(x, 4) for x in clustered_v if bx0 + 0.02 < x < bx1 - 0.02]

                    h_rows_raw = [round(hl['y'], 4) for hl in best_block[1:-1]]
                    h_rows = []
                    for r in sorted(h_rows_raw):
                        if not h_rows or (r - h_rows[-1]) > 0.015:
                            h_rows.append(r)

                    return {
                        'rect': (bx0, by0, bx1, by1),
                        'cols': v_cols,
                        'rows': h_rows
                    }

        except Exception as cv_err:
            logger.debug(f"OpenCV line detection skipped: {cv_err}")

        # =============================================================
        # TIER 3: Layout & Date-Anchored Word Bounds (Fallback)
        # =============================================================
        if True:  # Tier 3 applies to both scanned and selectable PDFs
            lines_by_y = {}
            for w in raw_words:
                x0, y0, x1, y1, text = w[:5]
                y_mid = (y0 + y1) / 2.0
                matched = None
                for ly in lines_by_y:
                    if abs(ly - y_mid) < 3.0:
                        matched = ly
                        break
                if matched is None:
                    lines_by_y[y_mid] = [w]
                else:
                    lines_by_y[matched].append(w)

            date_lines = []
            sorted_ys = sorted(lines_by_y.keys())
            for y in sorted_ys:
                lw = sorted(lines_by_y[y], key=lambda x: x[0])
                if lw and lw[0][0] < page_w * 0.4:
                    t0 = lw[0][4].strip()
                    if DATE_REGEX.match(t0):
                        date_lines.append((y, lw))
                    elif len(lw) > 1 and DATE_REGEX.match((t0 + " " + lw[1][4]).strip()):
                        date_lines.append((y, lw))
                    elif len(lw) > 2 and DATE_REGEX.match((t0 + " " + lw[1][4] + " " + lw[2][4]).strip()):
                        date_lines.append((y, lw))

            if not date_lines:
                return None

            first_date_y = date_lines[0][0]
            last_date_y = date_lines[-1][0]

            header_y = max(0.0, first_date_y - 20.0)
            for y in sorted_ys:
                if first_date_y - 45.0 <= y < first_date_y:
                    header_y = max(0.0, min([w[1] for w in lines_by_y[y]]) - 4.0)
                    break

            bottom_y = max([w[3] for w in date_lines[-1][1]]) + 4.0
            for y in sorted_ys:
                if last_date_y < y <= last_date_y + 110.0:
                    lw = lines_by_y[y]
                    line_str = ' '.join([w[4] for w in lw]).lower()
                    if any(k in line_str for k in ['page ', 'statement generated', 'total', 'closing balance', 'disclaimer']):
                        break
                    table_like = any(w[0] > page_w * 0.15 for w in lw)
                    if table_like:
                        bottom_y = max(bottom_y, max([w[3] for w in lw]) + 4.0)

            table_words = [w for w in raw_words if header_y - 4.0 <= (w[1] + w[3]) / 2.0 <= bottom_y + 4.0]
            if not table_words:
                return None

            min_x = max(0.0, min(w[0] for w in table_words) - 4.0)
            max_x = min(page_w, max(w[2] for w in table_words) + 4.0)

            row_splitters = []
            for i in range(len(date_lines) - 1):
                mid_y = (date_lines[i][0] + date_lines[i + 1][0]) / 2.0
                row_splitters.append(round(mid_y / page_h, 4))

            word_x0s = [w[0] for w in table_words]
            col_starts = sorted(cluster_coords(word_x0s, tolerance=15))
            v_cols = [round(c / page_w, 4) for c in col_starts if min_x + 10 < c < max_x - 10]

            return {
                'rect': (round(min_x / page_w, 4), round(header_y / page_h, 4), round(max_x / page_w, 4), round(bottom_y / page_h, 4)),
                'cols': v_cols,
                'rows': row_splitters
            }

        return None
    except Exception as e:
        logger.error(f"Error in auto-detecting table: {e}")
        return None

def adapt_template_to_page(doc, ref_box, page_num):
    """
    Dynamically adapts reference column splitters and horizontal margins
    to page_num, calculating page-specific top/bottom bounds and horizontal rows.
    """
    try:
        p_doc = doc[page_num]
        cur_w = p_doc.rect.width
        cur_h = p_doc.rect.height
        words = p_doc.get_text('words')

        ref_cols = list(ref_box.get('cols', []))
        ref_x0, ref_y0, ref_x1, ref_y1 = ref_box['rect']

        SUMMARY_KWS = ['statement summary', 'summary :', 'opening balance', 'dr count', 'cr count', 'total :', 'generated on:', 'page :']
        HEADER_KEYWORDS_LOCAL = ['date', 'narration', 'description', 'particulars', 'chq', 'ref', 'value', 'withdrawal', 'deposit', 'debit', 'credit', 'balance']

        lines_by_y = {}
        for w in words:
            y_mid = (w[1] + w[3]) / 2.0
            matched = None
            for ly in lines_by_y:
                if abs(ly - y_mid) < 3.5:
                    matched = ly
                    break
            if matched is None:
                lines_by_y[y_mid] = [w]
            else:
                lines_by_y[matched].append(w)

        best_header_y = None
        best_header_words = None
        max_kws = 0
        for y in sorted(lines_by_y.keys()):
            lw = sorted(lines_by_y[y], key=lambda x: x[0])
            line_text = ' '.join(w[4].lower() for w in lw)
            if any(s in line_text for s in SUMMARY_KWS):
                continue
            kw_count = sum(1 for kw in HEADER_KEYWORDS_LOCAL if re.search(r'\b' + kw + r'\b', line_text))
            if kw_count >= 3 and kw_count > max_kws:
                max_kws = kw_count
                best_header_y = y
                best_header_words = lw

        search_y_start = (best_header_y + 8) if best_header_y else (0.05 * cur_h)
        sorted_ys = [y for y in sorted(lines_by_y.keys()) if y > search_y_start]

        tx_blocks = []
        summary_y0 = None
        date_col_max_x = (ref_cols[0] if ref_cols else 0.25) * cur_w + 10.0

        for y in sorted_ys:
            lw = sorted(lines_by_y[y], key=lambda x: x[0])
            line_text = ' '.join(w[4].lower() for w in lw)
            if any(s in line_text for s in SUMMARY_KWS):
                if summary_y0 is None:
                    summary_y0 = lw[0][1]
                break
            if lw and DATE_REGEX.match(lw[0][4].strip()) and lw[0][0] < date_col_max_x:
                tx_blocks.append({'date_y': y, 'lines': [lw]})
            elif tx_blocks:
                tx_blocks[-1]['lines'].append(lw)

        h_rows = []
        for i in range(len(tx_blocks) - 1):
            curr_tx = tx_blocks[i]
            next_tx = tx_blocks[i + 1]
            curr_bottom = max(max(w[3] for w in l) for l in curr_tx['lines'])
            next_top = min(w[1] for w in next_tx['lines'][0])
            split_y = (curr_bottom + next_top) / (2.0 * cur_h)
            h_rows.append(round(split_y, 4))

        if best_header_words and tx_blocks:
            h_bot = max(w[3] for w in best_header_words)
            f_top = min(w[1] for w in tx_blocks[0]['lines'][0])
            by0 = round(max(0.02, (h_bot + f_top) / (2.0 * cur_h)), 4)
        elif tx_blocks:
            f_top = min(w[1] for w in tx_blocks[0]['lines'][0])
            by0 = round(max(0.02, (f_top - 6.0) / cur_h), 4)
        else:
            by0 = ref_y0

        if summary_y0 is not None:
            by1 = round(min(1.0, (summary_y0 - 2.0) / cur_h), 4)
        elif tx_blocks:
            l_bot = max(max(w[3] for w in l) for l in tx_blocks[-1]['lines'])
            by1 = round(min(1.0, (l_bot + 6.0) / cur_h), 4)
        else:
            by1 = ref_y1

        return {
            'rect': (ref_x0, by0, ref_x1, by1),
            'cols': [round(c, 4) for c in ref_cols if ref_x0 + 0.003 < c < ref_x1 - 0.003],
            'rows': h_rows,
            'origin': 'auto',
            'user_modified': False
        }
    except Exception as e:
        logger.error(f"Error adapting template to page {page_num}: {e}")
        return ref_box

