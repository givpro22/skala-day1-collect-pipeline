"""비동기 수집 모듈 — asyncio + httpx 로 3개 API를 동시에 호출한다.

순차 호출 대비 전체 소요 시간이 '가장 느린 한 개'로 수렴하는 것이 핵심이다.
개별 요청 실패가 전체 파이프라인을 중단시키지 않도록
asyncio.gather(return_exceptions=True) 와 요청 단위 예외 처리를 함께 사용한다.

실패 대응은 두 단계다.
    1) 재시도 : 같은 주소로 지수 백오프(0.5→1.0→2.0초) 재요청 — 일시적 오류용
    2) 미러   : 재시도까지 실패하면 대체 주소로 전환 — 지속적 차단·장애용
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from src.config import (
    API_ENDPOINTS,
    API_MIRRORS,
    MAX_ATTEMPTS,
    RAW_RESPONSE_JSON,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF,
)

logger = logging.getLogger(__name__)


async def request_once(client: httpx.AsyncClient, url: str) -> tuple[Any | None, str]:
    """주소 1건을 1회 호출한다 (재시도·폴백 없음).

    Args:
        client: 재사용할 httpx 비동기 클라이언트
        url   : 요청 주소

    Returns:
        (payload, "") 성공 / (None, 실패 사유) 실패
    """
    try:
        response = await client.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json(), ""
    except httpx.HTTPStatusError as e:  # 4xx·5xx 응답
        return None, f"HTTP {e.response.status_code}"
    except httpx.TimeoutException:  # 타임아웃
        return None, f"timeout({REQUEST_TIMEOUT}s)"
    except httpx.HTTPError as e:  # 연결 실패 등 나머지 통신 오류
        return None, f"{type(e).__name__}: {e}"
    except json.JSONDecodeError as e:  # 본문이 JSON이 아닌 경우
        return None, f"JSON 파싱 실패: {e}"


async def fetch_with_retry(
    client: httpx.AsyncClient,
    name: str,
    url: str,
    max_attempts: int = MAX_ATTEMPTS,
) -> tuple[Any | None, str, int]:
    """실패 시 지수 백오프로 재시도하며 같은 주소를 호출한다.

    네트워크 오류·5xx는 대개 일시적이므로, 즉시 포기하지 않고 간격을 늘려 가며
    다시 시도한다. 간격을 늘리는 이유는 장애 중인 서버에 요청을 몰아주지 않기 위해서다.

    Args:
        client      : 재사용할 httpx 비동기 클라이언트
        name        : API 식별자 (로그 표기용)
        url         : 요청 주소
        max_attempts: 최대 시도 횟수 (최초 1회 포함)

    Returns:
        (payload, 실패 사유, 시도 횟수)
    """
    reason = ""
    for attempt in range(1, max_attempts + 1):
        payload, reason = await request_once(client, url)
        if payload is not None:
            if attempt > 1:
                logger.info(f"[{name}] {attempt}회차 재시도에서 성공")
            return payload, "", attempt

        if attempt < max_attempts:
            wait = RETRY_BACKOFF * 2 ** (attempt - 1)  # 0.5 → 1.0 → 2.0 …
            logger.warning(
                f"[{name}] {attempt}/{max_attempts}회차 실패({reason}) "
                f"— {wait:.1f}s 후 재시도"
            )
            await asyncio.sleep(wait)

    return None, reason, max_attempts


async def fetch(client: httpx.AsyncClient, name: str, url: str) -> dict[str, Any]:
    """단일 API를 수집한다 — 재시도 후에도 실패하면 미러 주소로 전환한다.

    Args:
        client: 재사용할 httpx 비동기 클라이언트
        name  : API 식별자 (weather / country / ip)
        url   : 1차 요청 주소

    Returns:
        {'name', 'ok', 'elapsed', 'attempts', 'source', 'data'} 형태의 dict.
        실패 시 ok=False 이며 'error' 키에 사유가 담긴다.
        source 는 실제로 응답을 받아낸 주소로, 미러 사용 여부를 사후에 확인할 수 있다.
    """
    started = time.perf_counter()
    payload, reason, attempts = await fetch_with_retry(client, name, url)
    source = url

    # 1차 주소가 끝내 실패했고 미러가 정의돼 있으면 대체 주소로 한 번 더 시도한다
    mirror = API_MIRRORS.get(name)
    if payload is None and mirror:
        logger.warning(f"[{name}] 1차 주소 실패({reason}) — 미러로 전환: {mirror}")
        payload, mirror_reason, mirror_attempts = await fetch_with_retry(
            client, f"{name}:mirror", mirror
        )
        attempts += mirror_attempts
        if payload is not None:
            source = mirror
        else:
            reason = f"1차 {reason} / 미러 {mirror_reason}"

    elapsed = time.perf_counter() - started
    if payload is None:
        logger.error(f"[{name}] 수집 실패 ({elapsed:.2f}s, {attempts}회 시도): {reason}")
        return {
            "name": name,
            "ok": False,
            "elapsed": elapsed,
            "attempts": attempts,
            "error": reason,
        }

    via = "" if source == url else " (미러 사용)"
    logger.info(f"[{name}] 수집 성공 ({elapsed:.2f}s, {attempts}회 시도){via}")
    return {
        "name": name,
        "ok": True,
        "elapsed": elapsed,
        "attempts": attempts,
        "source": source,
        "data": payload,
    }


async def fetch_all(endpoints: dict[str, str] | None = None) -> dict[str, Any]:
    """모든 API를 asyncio.gather() 로 동시에 호출한다.

    Args:
        endpoints: {식별자: URL} 매핑. 기본값은 config.API_ENDPOINTS.

    Returns:
        {식별자: fetch() 결과} 매핑
    """
    endpoints = endpoints or API_ENDPOINTS
    started = time.perf_counter()

    # follow_redirects=True: http 요청이 https로 301 리다이렉트되는 API가 있어
    # 리다이렉트를 추적하지 않으면 본문이 비어 JSON 파싱에 실패한다
    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = [fetch(client, name, url) for name, url in endpoints.items()]
        # return_exceptions=True: 일부 실패해도 나머지 결과를 그대로 회수한다
        results = await asyncio.gather(*tasks, return_exceptions=True)

    total = time.perf_counter() - started
    collected: dict[str, Any] = {}
    for name, result in zip(endpoints, results):
        if isinstance(result, BaseException):  # gather가 잡아낸 예기치 못한 예외
            logger.error(f"[{name}] 예기치 못한 예외: {result}")
            collected[name] = {"name": name, "ok": False, "error": str(result)}
        else:
            collected[name] = result

    sequential = sum(r.get("elapsed", 0) for r in collected.values())
    success = sum(1 for r in collected.values() if r.get("ok"))
    logger.info(
        f"동시 수집 완료: {success}/{len(endpoints)}건 성공 | "
        f"실제 {total:.2f}s (순차 합산 추정 {sequential:.2f}s)"
    )
    return collected


def save_raw(responses: dict[str, Any]) -> None:
    """수집한 원본 응답을 JSON으로 보관한다 (재현성 확보용)."""
    RAW_RESPONSE_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(RAW_RESPONSE_JSON, "w", encoding="utf-8") as f:
        json.dump(responses, f, ensure_ascii=False, indent=2)
    logger.info(f"원본 응답 저장: {RAW_RESPONSE_JSON.name}")


def collect() -> dict[str, Any]:
    """이벤트 루프를 시작해 수집을 실행하고 원본을 저장한다."""
    responses = asyncio.run(fetch_all())
    save_raw(responses)
    return responses
