"""비동기 수집 모듈 — asyncio + httpx 로 3개 API를 동시에 호출한다.

순차 호출 대비 전체 소요 시간이 '가장 느린 한 개'로 수렴하는 것이 핵심이다.
개별 요청 실패가 전체 파이프라인을 중단시키지 않도록
asyncio.gather(return_exceptions=True) 와 요청 단위 예외 처리를 함께 사용한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from src.config import API_ENDPOINTS, RAW_RESPONSE_JSON, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)


async def fetch(client: httpx.AsyncClient, name: str, url: str) -> dict[str, Any]:
    """단일 API를 호출해 JSON 응답을 반환한다.

    Args:
        client: 재사용할 httpx 비동기 클라이언트
        name  : API 식별자 (weather / country / ip)
        url   : 요청 주소

    Returns:
        {'name', 'ok', 'elapsed', 'data'} 형태의 dict.
        실패 시 ok=False 이며 'error' 키에 사유가 담긴다.
    """
    started = time.perf_counter()
    try:
        response = await client.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as e:  # 4xx·5xx 응답
        reason = f"HTTP {e.response.status_code}"
    except httpx.TimeoutException:  # 타임아웃
        reason = f"timeout({REQUEST_TIMEOUT}s)"
    except httpx.HTTPError as e:  # 연결 실패 등 나머지 통신 오류
        reason = f"{type(e).__name__}: {e}"
    except json.JSONDecodeError as e:  # 본문이 JSON이 아닌 경우
        reason = f"JSON 파싱 실패: {e}"
    else:
        elapsed = time.perf_counter() - started
        logger.info(f"[{name}] 수집 성공 ({elapsed:.2f}s)")
        return {"name": name, "ok": True, "elapsed": elapsed, "data": payload}

    elapsed = time.perf_counter() - started
    logger.error(f"[{name}] 수집 실패 ({elapsed:.2f}s): {reason}")
    return {"name": name, "ok": False, "elapsed": elapsed, "error": reason}


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
