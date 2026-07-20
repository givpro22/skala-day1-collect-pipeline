"""파이프라인 단위 테스트 — 네트워크 없이도 검증 로직을 확인한다.

API 호출은 외부 환경에 의존하므로 테스트에서는 고정된 샘플 응답(fixture)을 쓴다.
"""

from __future__ import annotations

import asyncio

import httpx
import pandas as pd
import pytest
from pydantic import ValidationError

from src import collect
from src.schema import CountryInfo, IpInfo, WeatherRecord
from src.storage import to_dataframe
from src.transform import (
    build_records,
    extract_weather_rows,
    normalize_ip_payload,
    validate_ip,
    validate_weather,
)


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


# ------------------------------------------------------------------------------
# 재시도 · 미러 폴백 — httpx.MockTransport로 네트워크 없이 실패 상황을 재현한다
# ------------------------------------------------------------------------------
IPWHOIS_SAMPLE = {
    "ip": "8.8.8.8",
    "country": "United States",
    "city": "San Jose",
    "latitude": 37.33,
    "longitude": -121.89,
    "connection": {"isp": "Google LLC"},
}


def make_client(handler) -> httpx.AsyncClient:
    """요청을 실제로 보내지 않고 handler가 응답을 흉내 내는 클라이언트를 만든다."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """재시도 대기를 건너뛰어 테스트가 백오프 시간만큼 지연되지 않게 한다."""

    async def instant(_seconds: float) -> None:
        """asyncio.sleep 을 대신해 즉시 반환한다."""
        return None

    monkeypatch.setattr(collect.asyncio, "sleep", instant)


def test_fetch_with_retry_succeeds_after_failures() -> None:
    """앞선 시도가 실패해도 재시도 안에서 성공하면 정상 응답을 돌려줘야 한다."""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        """1·2회차는 503, 3회차부터 정상 응답을 돌려주는 가짜 서버."""
        calls["n"] += 1
        if calls["n"] < 3:  # 1·2회차는 서버 오류
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    async def run() -> tuple:
        """이벤트 루프 안에서 재시도 수집을 한 번 실행한다."""
        async with make_client(handler) as client:
            return await collect.fetch_with_retry(client, "test", "http://example.test")

    payload, reason, attempts = asyncio.run(run())
    assert payload == {"ok": True}
    assert reason == ""
    assert attempts == 3  # 3회차에 성공


def test_fetch_with_retry_gives_up_after_max_attempts() -> None:
    """계속 실패하면 최대 시도 횟수까지만 시도하고 사유를 반환해야 한다."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """몇 번을 요청해도 500만 돌려주는 가짜 서버."""
        return httpx.Response(500)

    async def run() -> tuple:
        """최대 시도 횟수를 2회로 제한해 실행한다."""
        async with make_client(handler) as client:
            return await collect.fetch_with_retry(
                client, "test", "http://example.test", max_attempts=2
            )

    payload, reason, attempts = asyncio.run(run())
    assert payload is None
    assert "HTTP 500" in reason
    assert attempts == 2


def test_fetch_falls_back_to_mirror() -> None:
    """1차 주소가 끝내 실패하면 미러 주소로 전환해 수집을 성공시켜야 한다."""

    def handler(request: httpx.Request) -> httpx.Response:
        """1차 주소는 연결 실패시키고 미러 주소만 응답하는 가짜 서버."""
        if request.url.host == "ip-api.com":  # 1차 주소는 항상 연결 실패
            raise httpx.ConnectTimeout("blocked", request=request)
        return httpx.Response(200, json=IPWHOIS_SAMPLE)

    async def run() -> dict:
        """1차 주소로 수집을 시작해 미러 전환까지 거치게 한다."""
        async with make_client(handler) as client:
            return await collect.fetch(client, "ip", "http://ip-api.com/json/8.8.8.8")

    result = asyncio.run(run())
    assert result["ok"] is True
    assert "ipwho.is" in result["source"]  # 미러에서 받아왔음이 기록된다


def test_normalize_ip_payload_unifies_providers() -> None:
    """공급자별 키 차이가 IpInfo 스키마 기준으로 통일되어야 한다."""
    normalized = normalize_ip_payload(IPWHOIS_SAMPLE)
    assert normalized["query"] == "8.8.8.8"
    assert normalized["lat"] == 37.33
    assert normalized["isp"] == "Google LLC"

    info, errors = validate_ip(IPWHOIS_SAMPLE)  # 미러 응답도 검증을 통과한다
    assert errors == []
    assert info is not None and info.city == "San Jose"
