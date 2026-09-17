from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from .config import FILE_SPECS, FileSpec


class WorkbookValidationError(ValueError):
    pass


HEADER_SCAN_ROWS = 12


@dataclass
class ParsedWorkbook:
    file_type: str
    label: str
    filename: str
    dataframe: pd.DataFrame
    warnings: list[str]
    sheet_name: str | int = 0
    header_row: int = 0
    original_row_count: int = 0


def clean_column_name(value: object) -> str:
    text = str(value).strip()
    text = re.sub(r"[\s\u3000]+", "", text)
    return text.replace("（", "(").replace("）", ")")


def normalize_warehouse_name(value: object) -> str:
    text = re.sub(r"[\s\u3000]+", "", str(value).strip())
    if text in {"", "nan", "None"}:
        return ""
    core = re.sub(r"(?:RDC|DC|仓库|仓)$", "", text, flags=re.IGNORECASE)
    return f"{core}仓库" if core != text and core else text


def _header_score(values: tuple[object, ...], filename: str, sheet_name: str) -> int:
    columns = {clean_column_name(value).lower() for value in values if value is not None and str(value).strip()}
    best_score = 0
    for spec in FILE_SPECS.values():
        lookup = _alias_lookup(spec)
        matched_fields = {lookup[column] for column in columns if column in lookup}
        required_match = sum(field in matched_fields for field in spec.required_fields)
        filename_bonus = 1000 if any(
            keyword.lower() in filename.lower() for keyword in spec.filename_keywords
        ) else 0
        sheet_bonus = 500 if any(
            keyword.lower() in sheet_name.lower() for keyword in spec.filename_keywords
        ) else 0
        best_score = max(
            best_score,
            filename_bonus + sheet_bonus + required_match * 100 + len(matched_fields) * 10,
        )
    return best_score


def _select_sheet_and_header(path: Path) -> tuple[str | int, int, int]:
    if path.suffix.lower() != ".xlsx":
        return 0, 0, 0

    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return 0, 0, 0

    best_sheet: str | int = 0
    best_header = 0
    best_sheet_index = 0
    best_score = 0
    try:
        for sheet_index, sheet in enumerate(workbook.worksheets):
            for row_index, values in enumerate(
                sheet.iter_rows(min_row=1, max_row=HEADER_SCAN_ROWS, values_only=True)
            ):
                score = _header_score(values, path.name, sheet.title)
                if score > best_score:
                    best_sheet = sheet.title
                    best_header = row_index
                    best_sheet_index = sheet_index
                    best_score = score
    finally:
        workbook.close()
    return best_sheet, best_header, best_sheet_index


def read_workbook(path: Path) -> pd.DataFrame:
    sheet_name, header_row, sheet_index = _select_sheet_and_header(path)
    try:
        frame = pd.read_excel(path, sheet_name=sheet_name, header=header_row, dtype=object)
    except Exception as exc:  # pandas/openpyxl error wording is not user friendly
        raise WorkbookValidationError(f"无法读取Excel：{exc}") from exc
    frame.columns = [clean_column_name(c) for c in frame.columns]
    frame = frame.dropna(how="all").reset_index(drop=True)
    frame.attrs["source_sheet"] = sheet_name
    frame.attrs["source_sheet_index"] = sheet_index
    frame.attrs["header_row"] = header_row
    return frame


def _alias_lookup(spec: FileSpec) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical, aliases in spec.field_aliases.items():
        for alias in aliases:
            lookup[clean_column_name(alias).lower()] = canonical
    return lookup


def detect_file_type(filename: str, frame: pd.DataFrame) -> str:
    lowered = filename.lower()
    keyword_matches = [
        key for key, spec in FILE_SPECS.items()
        if any(keyword.lower() in lowered for keyword in spec.filename_keywords)
    ]
    if len(keyword_matches) == 1:
        return keyword_matches[0]

    columns = {clean_column_name(c).lower() for c in frame.columns}
    scored: list[tuple[int, str]] = []
    for key, spec in FILE_SPECS.items():
        lookup = _alias_lookup(spec)
        matched_fields = {lookup[c] for c in columns if c in lookup}
        required_match = sum(field in matched_fields for field in spec.required_fields)
        optional_match = len(matched_fields)
        scored.append((required_match * 10 + optional_match, key))
    scored.sort(reverse=True)
    if not scored or scored[0][0] == 0 or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        raise WorkbookValidationError("无法识别文件类型，请在文件名中保留业务关键字。")
    return scored[0][1]


def standardize_columns(frame: pd.DataFrame, spec: FileSpec) -> tuple[pd.DataFrame, list[str]]:
    rename_map: dict[str, str] = {}
    warnings: list[str] = []
    normalized_columns = {clean_column_name(column).lower(): column for column in frame.columns}
    for canonical, aliases in spec.field_aliases.items():
        matched = [normalized_columns[clean_column_name(alias).lower()] for alias in aliases if clean_column_name(alias).lower() in normalized_columns]
        if matched:
            rename_map[matched[0]] = canonical
        if len(matched) > 1:
            ignored = "、".join(str(item) for item in matched[1:])
            warnings.append(f"字段“{canonical}”存在多个候选，已优先使用“{matched[0]}”，忽略：{ignored}。")

    standardized = frame.rename(columns=rename_map)
    missing = [field for field in spec.required_fields if field not in standardized.columns]
    if missing:
        readable = "、".join(missing)
        raise WorkbookValidationError(f"缺少必需字段：{readable}")
    return standardized, warnings


def parse_workbook(path: Path) -> ParsedWorkbook:
    frame = read_workbook(path)
    source_sheet = frame.attrs.get("source_sheet", 0)
    source_sheet_index = int(frame.attrs.get("source_sheet_index", 0))
    header_row = int(frame.attrs.get("header_row", 0))
    file_type = detect_file_type(path.name, frame)
    spec = FILE_SPECS[file_type]
    standardized, warnings = standardize_columns(frame, spec)
    if source_sheet_index != 0 or header_row != 0:
        warnings.insert(0, f"已自动使用工作表“{source_sheet}”第{header_row + 1}行作为表头。")
    return ParsedWorkbook(
        file_type, spec.label, path.name, standardized, warnings,
        source_sheet, header_row, len(frame),
    )


def numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(0.0, index=frame.index, dtype="float64")
    text = frame[column].astype(str).str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(text, errors="coerce").fillna(0.0)


def date_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns]")
    return pd.to_datetime(frame[column], errors="coerce").dt.normalize()


def text_series(frame: pd.DataFrame, column: str, default: str = "") -> pd.Series:
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype="object")
    values = frame[column].fillna(default).astype(str).str.strip()
    return values.replace({"nan": default, "None": default})
