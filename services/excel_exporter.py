"""
services/excel_exporter.py - Formatted Excel workbook exporter for bank statement transactions.
"""
import os
import re
import pandas as pd
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

def format_premium_worksheet(ws):
    header_font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    
    data_font = Font(name="Segoe UI", size=10, color="0F172A")
    alt_row_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    white_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
    
    thin_border_side = Side(style="thin", color="CBD5E1")
    header_border_side = Side(style="medium", color="0F172A")
    
    cell_border = Border(
        left=thin_border_side, right=thin_border_side,
        top=thin_border_side, bottom=thin_border_side
    )
    header_border = Border(
        left=thin_border_side, right=thin_border_side,
        top=thin_border_side, bottom=header_border_side
    )

    # 1. Format Header Row
    ws.row_dimensions[1].height = 28.0
    for col_idx in range(1, ws.max_column + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = header_border
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # 2. Format Data Rows
    for row_idx in range(2, ws.max_row + 1):
        ws.row_dimensions[row_idx].height = 22.0
        is_alt = (row_idx % 2 == 1)
        row_fill = alt_row_fill if is_alt else white_fill
        
        for col_idx in range(1, ws.max_column + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.font = data_font
            cell.fill = row_fill
            cell.border = cell_border
            
            header_name = str(ws.cell(row=1, column=col_idx).value or '').lower()
            val_str = str(cell.value or '').strip()
            
            if any(k in header_name for k in ['date', 'txn date', 'value date']):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            elif any(k in header_name for k in ['amount', 'withdrawal', 'deposit', 'balance', 'debit', 'credit']):
                try:
                    num_val = float(val_str.replace(',', ''))
                    cell.value = num_val
                    cell.number_format = '#,##0.00'
                except Exception:
                    pass
                cell.alignment = Alignment(horizontal="right", vertical="center")
            elif any(k in header_name for k in ['type', 'cr/dr', 'd/c', 'dr/cr']):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")

    # 3. Dynamic Column Widths
    for col in ws.columns:
        col_letter = col[0].column_letter
        max_len = max(len(str(cell.value or '')) for cell in col)
        header_text = str(col[0].value or '').lower()
        
        if any(k in header_text for k in ['description', 'particular', 'narration']):
            col_w = min(max(max_len + 6, 35), 65)
        elif 'date' in header_text:
            col_w = max(max_len + 4, 16)
        elif any(k in header_text for k in ['amount', 'balance', 'withdrawal', 'deposit']):
            col_w = max(max_len + 4, 18)
        elif 'type' in header_text:
            col_w = max(max_len + 4, 12)
        else:
            col_w = max(max_len + 5, 15)
            
        ws.column_dimensions[col_letter].width = col_w


def export_transactions_to_excel(transactions, output_path, consolidate=True, rename_dict=None):
    if not transactions:
        raise ValueError("No transactions to export.")

    df = pd.DataFrame(transactions)
    rename_dict = rename_dict or {}

    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        if consolidate:
            df_write = df.copy()
            if 'Source' in df_write.columns:
                df_write['Source'] = df_write['Source'].apply(lambda x: os.path.basename(str(x)))
            if rename_dict:
                df_write = df_write.rename(columns=rename_dict)
            df_write.to_excel(writer, sheet_name='Consolidated', index=False)
            format_premium_worksheet(writer.sheets['Consolidated'])
        else:
            if 'Source' not in df.columns:
                df['Source'] = 'Transactions'
            grouped = df.groupby('Source')
            for src, group in grouped:
                df_sheet = group.drop(columns=['Source']).copy() if len(grouped) > 1 else group.copy()
                if rename_dict:
                    df_sheet = df_sheet.rename(columns=rename_dict)
                
                sheet_name = os.path.basename(str(src))
                sheet_name = re.sub(r'[\\*?:/\[\]]', '', sheet_name)
                if len(sheet_name) > 30:
                    sheet_name = sheet_name[:30]
                if not sheet_name:
                    sheet_name = 'Sheet1'
                
                df_sheet.to_excel(writer, sheet_name=sheet_name, index=False)
                format_premium_worksheet(writer.sheets[sheet_name])

        # Apply official author metadata
        try:
            from utils import apply_workbook_watermark
            apply_workbook_watermark(writer.book)
        except Exception:
            try:
                wb = writer.book
                wb.properties.creator = "Vishwas Upponi"
                wb.properties.lastModifiedBy = "Vishwas Upponi"
                wb.properties.title = "Vertex Data Export"
                wb.properties.subject = "Author: Vishwas Upponi"
                wb.properties.description = "Generated by Vertex — Author: Vishwas Upponi"
                wb.properties.category = "Vertex"
            except Exception:
                pass

    return output_path
