"""변환·검증 모듈 — 원본 JSON에서 필요한 필드를 추출해 스키마로 검증한다.

Open-Meteo 응답은 컬럼형(시각/기온/강수확률이 각각 배열)이므로 행 단위로 재구성하고,
국가·IP 정보는 각 1건이라 모든 행에 공통 컬럼으로 부착해 하나의 표로 만든다.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from src.schema import CountryInfo, IpInfo, WeatherRecord

logger = logging.getLogger(__name__)


def extract_weather_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """컬럼형 hourly 응답을 행 단위 dict 리스트로 변환한다."""
    hourly = payload.get("hourly", {})
    return [
        {
            "time": time,
            "temperature_c": temperature,
            "precipitation_probability": probability,
        }
        for time, temperature, probability in zip(
            hourly.get("time", []),
            hourly.get("temperature_2m", []),
            hourly.get("precipitation_probability", []),
        )
    ]


def validate_weather(
    payload: dict[str, Any],
) -> tuple[list[WeatherRecord], list[dict[str, Any]]]:
    """시간대별 예보를 WeatherRecord로 검증해 valid / errors 로 분리한다."""
    valid: list[WeatherRecord] = []
    errors: list[dict[str, Any]] = []

    for idx, row in enumerate(extract_weather_rows(payload), start=1):
        try:
            valid.append(WeatherRecord(**row))
        except ValidationError as e:
            reason = "; ".join(
                f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()
            )
            errors.append({"source": "weather", "row": idx, "error": reason})
            logger.warning(f"[weather] {idx}행 검증 실패 — {reason}")

    logger.info(f"[weather] 검증 결과: 유효 {len(valid)}건 / 오류 {len(errors)}건")
    return valid, errors


def validate_country(payload: dict[str, Any]) -> tuple[CountryInfo | None, list[dict]]:
    """국가 정보를 CountryInfo로 검증한다 (응답 키 이름을 스키마에 맞게 매핑)."""
    currencies = payload.get("currencies") or [{}]
    candidate = {
        "name": payload.get("name"),
        "alpha3_code": payload.get("alpha3Code"),
        "capital": payload.get("capital"),
        "region": payload.get("region"),
        "population": payload.get("population"),
        "currency": currencies[0].get("code"),
    }
    try:
        info = CountryInfo(**candidate)
    except ValidationError as e:
        logger.warning(f"[country] 검증 실패 — {e.error_count()}건")
        return None, [{"source": "country", "row": 1, "error": str(e)}]

    logger.info(f"[country] 검증 성공: {info.name} / 수도 {info.capital}")
    return info, []


def normalize_ip_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """공급자마다 다른 IP 응답 키를 IpInfo 스키마 기준으로 통일한다.

    1차(ip-api)와 미러(ipwho.is)는 같은 정보를 다른 키 이름으로 준다.
    스키마를 공급자별로 나누는 대신 여기서 한 형태로 맞춰,
    어느 쪽에서 받아오든 하류 코드가 동일하게 동작하도록 한다.

        ip-api   : query / country / city / isp        / lat      / lon
        ipwho.is : ip    / country / city / connection.isp / latitude / longitude
    """
    if "query" in payload:  # ip-api 형태는 그대로 사용
        return payload

    connection = payload.get("connection") or {}
    return {
        "query": payload.get("ip"),
        "country": payload.get("country"),
        "city": payload.get("city"),
        "isp": connection.get("isp"),
        "lat": payload.get("latitude"),
        "lon": payload.get("longitude"),
    }


def validate_ip(payload: dict[str, Any]) -> tuple[IpInfo | None, list[dict]]:
    """IP 위치 정보를 IpInfo로 검증한다 (공급자별 키 차이는 먼저 정규화)."""
    try:
        info = IpInfo(**normalize_ip_payload(payload))
    except ValidationError as e:
        logger.warning(f"[ip] 검증 실패 — {e.error_count()}건")
        return None, [{"source": "ip", "row": 1, "error": str(e)}]

    logger.info(f"[ip] 검증 성공: {info.query} → {info.country}/{info.city}")
    return info, []


def build_records(
    responses: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """수집 결과 전체를 검증해 저장용 레코드와 오류 목록을 만든다.

    Args:
        responses: collect.fetch_all() 이 반환한 {식별자: 결과} 매핑

    Returns:
        (records, errors)
        records — 시간대별 예보에 국가·IP 정보를 공통 컬럼으로 붙인 dict 리스트
        errors  — {source, row, error} 형태의 검증 실패 목록
    """
    errors: list[dict[str, Any]] = []

    def payload_of(name: str) -> dict[str, Any] | None:
        """수집에 성공한 API의 본문만 돌려주고, 실패는 오류로 기록한 뒤 None을 반환한다.

        수집 단계에서 이미 실패한 응답을 다시 검증하면 같은 사고가
        '수집 실패'와 '필드 누락'으로 두 번 기록되므로 여기서 걸러낸다.
        """
        result = responses.get(name, {})
        if result.get("ok"):
            return result.get("data", {})
        errors.append(
            {"source": name, "row": 0, "error": f"수집 실패: {result.get('error')}"}
        )
        return None

    weather_payload = payload_of("weather")
    weather_rows, weather_errors = validate_weather(weather_payload or {})
    errors.extend(weather_errors)

    country_payload = payload_of("country")
    country, country_errors = (
        validate_country(country_payload) if country_payload else (None, [])
    )
    errors.extend(country_errors)

    ip_payload = payload_of("ip")
    ip_info, ip_errors = validate_ip(ip_payload) if ip_payload else (None, [])
    errors.extend(ip_errors)

    # 국가·IP 정보는 1건이므로 모든 예보 행에 공통 컬럼으로 부착한다
    context: dict[str, Any] = {}
    if country is not None:
        context |= {
            "country": country.name,
            "capital": country.capital,
            "population": country.population,
        }
    if ip_info is not None:
        context |= {"observer_ip": ip_info.query, "observer_city": ip_info.city}

    records = [row.model_dump() | context for row in weather_rows]
    logger.info(f"저장 대상 레코드 {len(records)}건 / 누적 오류 {len(errors)}건")
    return records, errors
