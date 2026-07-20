"""파이프라인 전역 설정 — API 주소·파일 경로·상수를 한곳에서 관리한다.

경로는 모두 프로젝트 루트 기준 절대경로로 계산하므로,
어느 디렉터리에서 실행하든 동일하게 동작한다.
"""

from __future__ import annotations

from pathlib import Path

# ------------------------------------------------------------------------------
# 경로
# ------------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"  # API 원본 응답(JSON)
PROCESSED_DIR = DATA_DIR / "processed"  # 검증 통과 데이터(CSV/Parquet)

RAW_RESPONSE_JSON = RAW_DIR / "api_responses.json"
CSV_PATH = PROCESSED_DIR / "weather_seoul.csv"
PARQUET_PATH = PROCESSED_DIR / "weather_seoul.parquet"
ERRORS_JSON = PROCESSED_DIR / "errors.json"

# 대용량 구간 성능 비교용 (수집량이 적어 포맷 차이가 드러나지 않을 때 사용)
SCALED_CSV_PATH = PROCESSED_DIR / "weather_scaled.csv"
SCALED_PARQUET_PATH = PROCESSED_DIR / "weather_scaled.parquet"

# ------------------------------------------------------------------------------
# 수집 대상 API (실습 지정 3종)
# ------------------------------------------------------------------------------
SEOUL_LAT, SEOUL_LON = 37.5665, 126.9780
TARGET_IP = "8.8.8.8"

API_ENDPOINTS: dict[str, str] = {
    # 서울 3일치 시간대별 기온·강수확률
    "weather": (
        "http://api.open-meteo.com/v1/forecast"
        f"?latitude={SEOUL_LAT}&longitude={SEOUL_LON}"
        "&hourly=temperature_2m,precipitation_probability"
        "&forecast_days=3&timezone=Asia/Seoul"
    ),
    # 한국 국가 정보 (http 요청 시 https로 301 리다이렉트되므로 추적 설정 필요)
    "country": "http://countries.dev/alpha/KOR",
    # IP 기반 지역 정보
    "ip": f"http://ip-api.com/json/{TARGET_IP}",
}

# 1차 주소가 차단·장애로 실패할 때 같은 정보를 제공하는 대체 주소.
# ip-api.com은 일부 네트워크(사내망·학내망)에서 80/443 포트가 막혀 접근되지 않으므로,
# 동일한 IP 지오로케이션 정보를 주는 ipwho.is를 미러로 둔다.
API_MIRRORS: dict[str, str] = {
    "ip": f"https://ipwho.is/{TARGET_IP}",
}

# ------------------------------------------------------------------------------
# 동작 파라미터
# ------------------------------------------------------------------------------
REQUEST_TIMEOUT = 5.0  # 개별 요청 타임아웃(초) — 재시도가 있으므로 짧게 잡는다
MAX_ATTEMPTS = 3  # 요청 1건당 최대 시도 횟수 (최초 1회 + 재시도 2회)
RETRY_BACKOFF = 0.5  # 지수 백오프 기준 대기(초): 0.5 → 1.0 → 2.0

BENCHMARK_REPEAT = 20  # 저장·로딩 성능 측정 반복 횟수 (두 포맷 동일 적용)
SCALE_FACTOR = 500  # 대용량 비교 시 수집 데이터를 복제할 배수
