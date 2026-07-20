"""저장·성능 비교 모듈 — 검증 통과 데이터를 CSV와 Parquet으로 저장하고
읽기/쓰기 시간과 파일 크기를 동일 조건으로 측정해 비교한다.

두 포맷의 반복 횟수(BENCHMARK_REPEAT)를 통일해야 공정한 비교가 된다.
"""

from __future__ import annotations

import json
import logging
import timeit
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import BENCHMARK_REPEAT, CSV_PATH, ERRORS_JSON, PARQUET_PATH

logger = logging.getLogger(__name__)


def to_dataframe(records: list[dict[str, Any]]) -> pd.DataFrame:
    """검증 통과 레코드 리스트를 DataFrame으로 변환한다."""
    if not records:
        raise ValueError("저장할 레코드가 없습니다.")
    return pd.DataFrame(records)


def save_errors(errors: list[dict[str, Any]], path: Path = ERRORS_JSON) -> None:
    """검증·수집 오류 목록을 JSON으로 저장한다 (한글 보존)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(errors, f, ensure_ascii=False, indent=2)
    logger.info(f"오류 리포트 저장: {path.name} ({len(errors)}건)")


def benchmark(
    df: pd.DataFrame,
    csv_path: Path = CSV_PATH,
    parquet_path: Path = PARQUET_PATH,
    repeat: int = BENCHMARK_REPEAT,
) -> pd.DataFrame:
    """CSV·Parquet의 쓰기/읽기 시간과 파일 크기를 측정해 표로 반환한다.

    두 포맷에 동일한 repeat 값을 적용해야 공정한 비교가 된다.

    Args:
        df          : 저장할 데이터
        csv_path    : CSV 저장 경로
        parquet_path: Parquet 저장 경로
        repeat      : 두 포맷에 동일하게 적용할 반복 횟수

    Returns:
        format · write_ms · read_ms · size_kb 컬럼을 가진 비교표
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    # 포맷별 (쓰기 함수, 읽기 함수, 경로)를 묶어 동일한 절차로 측정한다
    targets = {
        "CSV": (
            lambda: df.to_csv(csv_path, index=False, encoding="utf-8"),
            lambda: pd.read_csv(csv_path),
            csv_path,
        ),
        "Parquet": (
            lambda: df.to_parquet(parquet_path, index=False),
            lambda: pd.read_parquet(parquet_path),
            parquet_path,
        ),
    }

    rows = []
    for name, (write, read, path) in targets.items():
        write()  # 읽기 측정 전에 파일이 반드시 존재하도록 1회 선행 저장
        write_sec = timeit.timeit(write, number=repeat) / repeat
        read_sec = timeit.timeit(read, number=repeat) / repeat
        rows.append(
            {
                "format": name,
                "write_ms": round(write_sec * 1000, 3),
                "read_ms": round(read_sec * 1000, 3),
                "size_kb": round(path.stat().st_size / 1024, 2),
            }
        )
        logger.info(f"[{name}] 저장 완료: {path.name}")

    return pd.DataFrame(rows)


def verify_saved(expected_rows: int) -> None:
    """저장한 두 파일을 다시 읽어 행 수가 일치하는지 확인한다."""
    csv_rows = len(pd.read_csv(CSV_PATH))
    parquet_rows = len(pd.read_parquet(PARQUET_PATH))
    if not csv_rows == parquet_rows == expected_rows:
        raise ValueError(
            f"재로딩 건수 불일치 — 기대 {expected_rows} / "
            f"CSV {csv_rows} / Parquet {parquet_rows}"
        )
    logger.info(f"재로딩 검증 통과: CSV·Parquet 모두 {expected_rows}건")
