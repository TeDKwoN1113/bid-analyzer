"""공공데이터포털(조달청) OpenAPI 호출."""
import time
import xml.etree.ElementTree as ET

import requests

from config import OPERATIONS, load_settings


class ApiError(Exception):
    pass


def _parse(resp: requests.Response) -> tuple[list[dict], int]:
    text = resp.text.strip()
    if text.startswith("<"):
        # 키 오류 등은 XML로 옴
        try:
            root = ET.fromstring(text)
            msg = root.findtext(".//returnAuthMsg") or root.findtext(".//resultMsg") or text[:300]
            code = root.findtext(".//returnReasonCode") or root.findtext(".//resultCode") or ""
        except ET.ParseError:
            msg, code = text[:300], ""
        raise ApiError(f"API 오류 [{code}] {msg}")
    try:
        data = resp.json()
    except ValueError:
        raise ApiError(f"응답을 읽을 수 없음: {text[:300]}")

    r = data.get("response", data)
    header = r.get("header", {})
    code = str(header.get("resultCode", "00"))
    if code not in ("00", "0", "000"):
        raise ApiError(f"API 오류 [{code}] {header.get('resultMsg', '')}")
    body = r.get("body", {}) or {}
    items = body.get("items") or []
    if isinstance(items, dict):
        items = items.get("item") or []
    if isinstance(items, dict):
        items = [items]
    total = int(body.get("totalCount") or len(items))
    return items, total


def call(op: str, params: dict, page: int = 1, rows: int | None = None) -> tuple[list[dict], int]:
    s = load_settings()
    if not s["service_key"]:
        raise ApiError("API 키가 없습니다. '설정' 화면에서 공공데이터포털 인증키를 입력하세요.")
    q = {
        "serviceKey": s["service_key"],
        "type": "json",
        "pageNo": page,
        "numOfRows": rows or s["num_of_rows"],
    }
    q.update({k: v for k, v in params.items() if v not in (None, "")})
    last = None
    for attempt in range(3):
        try:
            resp = requests.get(OPERATIONS[op], params=q, timeout=30)
            if resp.status_code >= 500:
                raise ApiError(f"서버 오류 HTTP {resp.status_code}")
            return _parse(resp)
        except (requests.RequestException, ApiError) as e:
            last = e
            if isinstance(e, ApiError) and "서버 오류" not in str(e):
                raise
            time.sleep(2 * (attempt + 1))
    raise ApiError(f"API 호출 실패: {last}")


def call_all(op: str, params: dict, max_pages: int = 50) -> list[dict]:
    """모든 페이지를 가져옴."""
    out, page = [], 1
    while page <= max_pages:
        items, total = call(op, params, page=page)
        out.extend(items)
        if not items or len(out) >= total:
            break
        page += 1
        time.sleep(0.2)
    return out


def raw_call(op: str, params: dict) -> str:
    """API 점검용: 원본 응답 텍스트."""
    s = load_settings()
    q = {"serviceKey": s["service_key"], "type": "json", "pageNo": 1, "numOfRows": 5}
    q.update({k: v for k, v in params.items() if v not in (None, "")})
    resp = requests.get(OPERATIONS[op], params=q, timeout=30)
    return f"HTTP {resp.status_code}\n{resp.text[:5000]}"
