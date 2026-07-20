"""Pydantic v2 스키마 — 수집한 JSON에서 필요한 필드만 뽑아 타입·범위를 검증한다.

API 응답은 언제든 형태가 바뀔 수 있으므로, 파이프라인 하류(저장·분석)로
넘어가기 전에 이 계층에서 잘못된 레코드를 걸러낸다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class WeatherRecord(BaseModel):
    """서울 시간대별 예보 1건 (Open-Meteo).

    temperature_c : 지구 기온의 물리적 범위를 벗어나면 수집 오류로 간주
    precipitation_probability : 확률이므로 0~100 범위
    """

    time: str = Field(min_length=1, description="예보 시각 (ISO8601)")
    temperature_c: float = Field(ge=-60, le=60, description="기온(℃)")
    precipitation_probability: int = Field(ge=0, le=100, description="강수확률(%)")

    @field_validator("time")
    @classmethod
    def check_iso_format(cls, value: str) -> str:
        """'YYYY-MM-DDTHH:MM' 형태인지 최소한으로 확인한다."""
        if "T" not in value or len(value) < 16:
            raise ValueError(f"ISO8601 형식이 아닙니다: {value!r}")
        return value


class CountryInfo(BaseModel):
    """국가 기본 정보 (Countries.dev)."""

    name: str = Field(min_length=1, description="국가명")
    alpha3_code: str = Field(min_length=3, max_length=3, description="ISO 3자리 코드")
    capital: str = Field(min_length=1, description="수도")
    region: str = Field(min_length=1, description="대륙")
    population: int = Field(gt=0, description="인구(0 초과)")
    currency: str | None = Field(default=None, description="통화 코드(선택)")


class IpInfo(BaseModel):
    """IP 기반 위치 정보 (ip-api)."""

    query: str = Field(min_length=1, description="조회 대상 IP")
    country: str = Field(min_length=1, description="국가")
    city: str | None = Field(default=None, description="도시(선택)")
    isp: str | None = Field(default=None, description="통신사(선택)")
    lat: float = Field(ge=-90, le=90, description="위도")
    lon: float = Field(ge=-180, le=180, description="경도")
