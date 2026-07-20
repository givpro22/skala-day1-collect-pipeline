# [Day 1 종합 실습] 데이터 수집 미니 파이프라인

공개 API 3종을 **비동기로 동시 수집** → **Pydantic v2 스키마 검증** → **CSV·Parquet 저장 및 성능 비교**까지 수행하는 미니 데이터 파이프라인입니다.

- 작성자: 광주캠퍼스 4반 박영서
- 작성일: 2026-07-20

---

## 1. 프로젝트 개요

```
수집(asyncio + httpx) → 검증(Pydantic v2) → 저장(CSV/Parquet) → 성능 비교 → 재로딩 검증
```

| 단계 | 내용 | 사용 기술 |
|---|---|---|
| 수집 | 3개 API 동시 호출, 실패 허용 | `asyncio.gather(return_exceptions=True)`, `httpx.AsyncClient` |
| 검증 | 필요한 필드만 추출해 타입·범위 확인 | Pydantic v2 `BaseModel`, `Field`, `field_validator` |
| 저장 | 검증 통과분을 두 포맷으로 저장 | pandas, pyarrow |
| 비교 | 쓰기·읽기 시간과 파일 크기 측정 | `timeit` (두 포맷 동일 반복 횟수) |

### 수집 대상 API

| 식별자 | API | 내용 |
|---|---|---|
| `weather` | Open-Meteo | 서울 3일치 시간대별 기온·강수확률 (72행) |
| `country` | Countries.dev | 한국(KOR) 국가 정보 |
| `ip` | ip-api | IP(8.8.8.8) 기반 지역 정보 |

---

## 2. 폴더 구조

```
광주캠퍼스_4반_박영서_day1종합실습/
├── src/
│   ├── config.py       # API 주소·경로·상수
│   ├── collect.py      # asyncio.gather() 기반 동시 수집
│   ├── schema.py       # Pydantic v2 검증 스키마 3종
│   ├── transform.py    # 응답 → 레코드 변환 및 검증
│   ├── storage.py      # CSV/Parquet 저장 및 성능 측정
│   └── pipeline.py     # 전체 흐름 실행 (진입점)
├── tests/
│   └── test_pipeline.py  # pytest 12건 (네트워크 없이 검증 로직 테스트)
├── data/
│   ├── raw/            # API 원본 응답 (.gitignore 처리)
│   └── processed/      # 검증 통과 데이터 CSV/Parquet, 오류 리포트
├── requirements.txt
├── pyproject.toml      # ruff·pytest 설정
├── .gitignore
└── README.md
```

---

## 3. 개발 환경 설정

```bash
# 1) 가상환경 생성 및 활성화
python3 -m venv .venv
source .venv/bin/activate

# 2) 패키지 설치
pip install -r requirements.txt
```

---

## 4. 실행 방법

```bash
# 파이프라인 실행 (프로젝트 루트에서)
python -m src.pipeline

# 테스트
pytest

# 코드 스타일 검사
ruff check .
ruff format --check .
```

---

## 5. 실행 결과 요약

### 비동기 수집
3개 API를 `asyncio.gather()`로 동시 호출합니다. 순차 호출이었다면 각 응답 시간의 합만큼 걸렸을 작업이, 가장 느린 한 건의 시간으로 수렴합니다.

### 스키마 검증
72건의 시간대별 예보가 `WeatherRecord`를 통과했습니다. 기온은 -60~60℃, 강수확률은 0~100% 범위로 제한해 API 응답이 비정상일 때 하류로 전파되지 않게 막았습니다.

### CSV vs Parquet 성능 비교

**수집 데이터 (72행)** — 반복 20회 평균

| format | write_ms | read_ms | size_kb |
|---|---|---|---|
| CSV | 0.610 | 0.605 | 4.29 |
| Parquet | 0.906 | 3.485 | 4.97 |

**확대 데이터 (36,000행)** — 동일 조건 재측정

| format | write_ms | read_ms | size_kb |
|---|---|---|---|
| CSV | 66.839 | 18.340 | 2,112.38 |
| Parquet | 8.202 | 1.449 | 9.58 |

→ 36,000행 기준 Parquet이 **읽기 12.7배·쓰기 8.1배 빠르고 파일 크기는 220배 작습니다.**

---

## 6. 결과 분석 및 의견

### 소량 데이터에서는 CSV가 더 빨랐다
72행 구간에서는 오히려 CSV의 읽기가 빨랐습니다. Parquet은 스키마 메타데이터를 읽고 컬럼 청크를 해석하는 고정 비용이 있어, 데이터가 작으면 이 오버헤드가 이득을 상회합니다. **"Parquet이 항상 빠르다"는 명제는 틀렸고, 손익분기점이 존재합니다.** 실습 자료의 "CSV 대비 10배 빠른 읽기"는 대용량을 전제한 수치임을 실측으로 확인했습니다.

### 파일 크기 220배 차이의 원인
확대 데이터는 `country`·`capital`·`population` 컬럼의 값이 모든 행에서 동일합니다. Parquet은 컬럼형 저장이라 이런 반복 값을 딕셔너리 인코딩으로 압축하지만, 행 기반인 CSV는 같은 문자열을 36,000번 그대로 기록합니다. **데이터 형태가 압축률을 좌우한다**는 점이 드러납니다.

### 개선 사항
- **재시도 로직**: 현재는 실패한 API를 오류로 기록만 합니다. 실습 자료 33쪽의 `@retry` 데코레이터를 적용해 일시적 네트워크 오류에 대응하면 수집 성공률을 높일 수 있습니다.
- **증분 수집**: 매 실행마다 전체를 새로 받습니다. 마지막 수집 시각을 기록해 변경분만 가져오면 API 호출량을 줄일 수 있습니다.
- **커버리지 측정**: `pytest --cov=src`로 테스트가 닿지 않는 경로(특히 `collect.py`의 예외 분기)를 확인해야 합니다. 현재 테스트는 네트워크 의존성을 피하려 검증 로직 위주로 작성돼 있습니다.
- **스키마 버전 관리**: API 응답 형태가 바뀌면 검증이 일괄 실패합니다. 스키마에 버전을 부여하고 실패율이 임계치를 넘으면 알림을 보내는 장치가 필요합니다.

### 알려진 제약
`ip-api.com`은 작성 환경의 네트워크에서 차단되어 타임아웃이 발생했습니다. 다만 `return_exceptions=True`와 요청 단위 예외 처리 덕분에 **나머지 2개 API 수집과 파이프라인 전체는 정상 완료**되며, 실패 내역은 `data/processed/errors.json`에 기록됩니다. 이는 일부 API 장애가 전체 파이프라인을 멈추지 않아야 한다는 설계 의도가 실제로 동작함을 보여줍니다.
