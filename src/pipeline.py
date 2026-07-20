"""
[Day 1 종합 실습] 데이터 수집 미니 파이프라인 — 실행 진입점
================================================================================
프로그램 개요
--------------------------------------------------------------------------------
공개 API 3종을 비동기로 동시 수집하고, Pydantic v2로 스키마를 검증한 뒤,
검증 통과 데이터를 CSV·Parquet 두 형식으로 저장해 읽기/쓰기 성능을 비교한다.

    수집(asyncio+httpx) → 검증(Pydantic v2) → 저장(CSV/Parquet) → 성능 비교

수집 대상 API
    1. Open-Meteo   : 서울 3일치 시간대별 기온·강수확률
    2. Countries.dev: 한국(KOR) 국가 정보
    3. ip-api       : IP(8.8.8.8) 기반 지역 정보

모듈 구성
    src/config.py    : API 주소·경로·상수
    src/collect.py   : asyncio.gather() 기반 동시 수집
    src/schema.py    : Pydantic v2 검증 스키마 3종
    src/transform.py : 응답 → 레코드 변환 및 검증
    src/storage.py   : CSV/Parquet 저장 및 성능 측정
    src/pipeline.py  : 전체 흐름 실행 (본 파일)

실행 방법
    python -m src.pipeline          # 프로젝트 루트에서 실행

변경 내역
--------------------------------------------------------------------------------
    v1.0  2026-07-20  박영서  최초 작성 (수집·검증·저장·성능비교 전 구간 구현)
    v1.1  2026-07-20  박영서  수집 재시도·미러 폴백 추가, 벤치마크 워밍업 보정

작성자 : 광주캠퍼스 4반 박영서
================================================================================
"""

from __future__ import annotations

import logging
import sys

import pandas as pd

from src.collect import collect
from src.config import (
    BENCHMARK_REPEAT,
    SCALE_FACTOR,
    SCALED_CSV_PATH,
    SCALED_PARQUET_PATH,
)
from src.storage import benchmark, save_errors, to_dataframe, verify_saved
from src.transform import build_records

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s|%(levelname)s|%(name)s|%(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("pipeline")


def section(title: str) -> None:
    """단계 구분선을 출력해 실행 결과 캡처의 가독성을 높인다."""
    print(f"\n{'=' * 70}\n {title}\n{'=' * 70}")


def main() -> int:
    """파이프라인 전 구간을 순서대로 실행한다.

    Returns:
        정상 종료 0, 실패 1 (셸에서 성공/실패 판별용)
    """
    # 1) 비동기 수집 -----------------------------------------------------------
    section("[1] 비동기 수집 — asyncio.gather() 로 3개 API 동시 호출")
    responses = collect()
    for name, result in responses.items():
        status = "성공" if result.get("ok") else f"실패({result.get('error')})"
        print(f"  {name:8s} : {status} ({result.get('elapsed', 0):.2f}s)")

    # 2) 스키마 검증 -----------------------------------------------------------
    section("[2] 스키마 검증 — Pydantic v2 로 타입·범위 확인")
    records, errors = build_records(responses)
    print(f"  검증 통과 {len(records)}건 / 오류 {len(errors)}건")
    if records:
        print(f"  첫 레코드: {records[0]}")
    for item in errors:
        print(f"  [오류] {item['source']} {item['row']}행 → {item['error']}")

    save_errors(errors)

    if not records:
        logger.error("검증 통과 데이터가 없어 저장 단계를 건너뜁니다.")
        return 1

    # 3) 저장 + 성능 비교 -------------------------------------------------------
    section("[3] 저장 및 성능 비교 — CSV vs Parquet")
    df = to_dataframe(records)
    print(f"  데이터 형태: {df.shape[0]}행 × {df.shape[1]}열")
    print(f"  컬럼: {list(df.columns)}\n")

    print(f"  ● 수집 데이터 ({len(df):,}행) — 반복 {BENCHMARK_REPEAT}회 평균")
    result = benchmark(df)
    print(result.to_string(index=False))

    # 수집량이 적으면 포맷 특성이 드러나지 않으므로, 같은 데이터를 복제해
    # 대용량 구간에서의 경향까지 함께 측정한다 (측정 조건은 동일하게 유지)
    print(f"\n  ● 확대 데이터 ({len(df) * SCALE_FACTOR:,}행) — 동일 조건 재측정")
    scaled_df = pd.concat([df] * SCALE_FACTOR, ignore_index=True)
    scaled = benchmark(scaled_df, SCALED_CSV_PATH, SCALED_PARQUET_PATH)
    print(scaled.to_string(index=False))

    # CSV 대비 Parquet이 몇 배 유리한지로 환산해 결과를 해석한다
    stat = scaled.set_index("format")
    gain = {
        key: stat.loc["CSV", key] / stat.loc["Parquet", key]
        for key in ("read_ms", "write_ms", "size_kb")
    }
    print(
        f"\n  → 확대 데이터({len(scaled_df):,}행) 기준 Parquet 우위: "
        f"읽기 {gain['read_ms']:.1f}배 빠름, 쓰기 {gain['write_ms']:.1f}배 빠름, "
        f"파일 크기 {gain['size_kb']:.1f}배 작음"
    )
    print(
        f"     ({len(df)}행 소량 구간에서는 CSV가 근소하게 빨랐다 — "
        "Parquet의 이점은 데이터가 커질수록 드러난다)"
    )

    # 4) 재로딩 검증 -----------------------------------------------------------
    section("[4] 저장 결과 재로딩 검증")
    verify_saved(len(records))
    print(f"  CSV·Parquet 모두 {len(records)}건으로 재로딩 확인")

    print("\n[OK] 파이프라인 정상 완료")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as e:
        logger.error(f"파이프라인 실패: {type(e).__name__}: {e}")
        raise SystemExit(1) from e
