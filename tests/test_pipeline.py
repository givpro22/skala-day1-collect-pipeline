"""파이프라인 단위 테스트 — 네트워크 없이도 검증 로직을 확인한다.

API 호출은 외부 환경에 의존하므로 테스트에서는 고정된 샘플 응답(fixture)을 쓴다.
"""

from __future__ import annotations

import pandas as pd
import pytest
from pydantic import ValidationError

from src.schema import CountryInfo, IpInfo, WeatherRecord
from src.storage import to_dataframe
from src.transform import build_records, extract_weather_rows, validate_weather


@pytest.fixture
def weather_payload() -> dict:
    """Open-Meteo 응답 형태의 샘플 (정상 2건 + 강수확률 범위 초과 1건)."""
    return {
        "hourly": {
            "time": ["2026-07-20T00:00", "2026-07-20T01:00", "2026-07-20T02:00"],
            "temperature_2m": [23.2, 22.9, 22.5],
            "precipitation_probability": [6, 15, 150],  # 150%는 범위 초과 → 오류
        }
    }


@pytest.fixture
def responses(weather_payload: dict) -> dict:
    """collect.fetch_all() 반환 형태의 샘플."""
    return {
        "weather": {"name": "weather", "ok": True, "elapsed": 0.1, "data": weather_payload},
        "country": {
            "name": "country",
            "ok": True,
            "elapsed": 0.1,
            "data": {
                "name": "Korea (Republic of)",
                "alpha3Code": "KOR",
                "capital": "Seoul",
                "region": "Asia",
                "population": 51780579,
                "currencies": [{"code": "KRW"}],
            },
        },
        "ip": {
            "name": "ip",
            "ok": True,
            "elapsed": 0.1,
            "data": {
                "query": "8.8.8.8",
                "country": "United States",
                "city": "Ashburn",
                "isp": "Google LLC",
                "lat": 39.03,
                "lon": -77.5,
            },
        },
    }


def test_weather_record_accepts_valid_row() -> None:
    """정상 범위 값은 검증을 통과해야 한다."""
    record = WeatherRecord(
        time="2026-07-20T00:00", temperature_c=23.2, precipitation_probability=6
    )
    assert record.temperature_c == 23.2


@pytest.mark.parametrize(
    "field,value",
    [
        ("precipitation_probability", 150),  # 0~100 초과
        ("temperature_c", 999),  # 물리적 범위 초과
        ("time", "2026-07-20"),  # ISO8601 형식 아님
    ],
)
def test_weather_record_rejects_invalid(field: str, value: object) -> None:
    """범위·형식을 벗어난 값은 ValidationError를 발생시켜야 한다."""
    row = {
        "time": "2026-07-20T00:00",
        "temperature_c": 23.2,
        "precipitation_probability": 6,
    }
    row[field] = value
    with pytest.raises(ValidationError):
        WeatherRecord(**row)


def test_country_requires_alpha3_length() -> None:
    """국가 코드는 3자리여야 한다."""
    with pytest.raises(ValidationError):
        CountryInfo(
            name="Korea",
            alpha3_code="KR",  # 2자리 → 실패
            capital="Seoul",
            region="Asia",
            population=51780579,
        )


def test_ip_info_rejects_out_of_range_latitude() -> None:
    """위도는 -90~90 범위를 벗어날 수 없다."""
    with pytest.raises(ValidationError):
        IpInfo(query="8.8.8.8", country="US", lat=120.0, lon=0.0)


def test_extract_weather_rows_zips_columns(weather_payload: dict) -> None:
    """컬럼형 응답이 행 단위로 정확히 재구성되어야 한다."""
    rows = extract_weather_rows(weather_payload)
    assert len(rows) == 3
    assert rows[0]["temperature_c"] == 23.2


def test_validate_weather_splits_valid_and_errors(weather_payload: dict) -> None:
    """범위를 벗어난 행만 errors로 분리되어야 한다."""
    valid, errors = validate_weather(weather_payload)
    assert len(valid) == 2
    assert len(errors) == 1
    assert errors[0]["source"] == "weather"


def test_build_records_attaches_context(responses: dict) -> None:
    """국가·IP 정보가 각 예보 행에 공통 컬럼으로 부착되어야 한다."""
    records, errors = build_records(responses)
    assert len(records) == 2  # 정상 2건
    assert len(errors) == 1  # 범위 초과 1건
    assert records[0]["capital"] == "Seoul"
    assert records[0]["observer_ip"] == "8.8.8.8"


def test_build_records_reports_failed_api(responses: dict) -> None:
    """수집 실패한 API는 오류 목록에 기록되어야 한다."""
    responses["ip"] = {"name": "ip", "ok": False, "error": "timeout(10.0s)"}
    _, errors = build_records(responses)
    assert any(item["source"] == "ip" for item in errors)


def test_to_dataframe_raises_on_empty() -> None:
    """저장할 레코드가 없으면 예외를 발생시켜야 한다."""
    with pytest.raises(ValueError):
        to_dataframe([])


def test_to_dataframe_builds_expected_shape(responses: dict) -> None:
    """레코드 리스트가 DataFrame으로 올바르게 변환되어야 한다."""
    records, _ = build_records(responses)
    df = to_dataframe(records)
    assert isinstance(df, pd.DataFrame)
    assert df.shape[0] == 2
    assert "temperature_c" in df.columns
