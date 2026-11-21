"""공공데이터포털(조달청) OpenAPI 호출."""
import time
import xml.etree.ElementTree as ET

import requests

from config import OPERATIONS, load_settings


class ApiError(Exception):
    pass


class QuotaError(ApiError):
    """하루 호출 한도 초과 - 내일 다시 이어서 하면 됨."""


class KeyError_(ApiError):
    """인증키 문제."""


_rows_ok = {}  # API별로 실제 허용되는 numOfRows 기억


def _raise(code: str, msg: str):
    code = str(code)
    if code == "22" or "LIMITED" in msg:
        raise QuotaError(f"하루 호출 한도 초과 [{code}] {msg}")
    if code in ("20", "30", "31", "32") or "SERVICE_KEY" in msg or "SERVICE KEY" in msg:
        raise KeyError_(f"인증키 오류 [{code}] {msg}")
    raise ApiError(f"API 오류 [{code}] {msg}")


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
        _raise(code, msg)
    try:
        data = resp.json()
    except ValueError:
        raise ApiError(f"응답을 읽을 수 없음: {text[:300]}")

    r = data.get("response", data)
    header = r.get("header", {})
    code = str(header.get("resultCode", "00"))
    if code not in ("00", "0", "000"):
        _raise(code, str(header.get("resultMsg", "")))
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
            # 타임아웃 등 네트워크 문제는 잠시 후 재시도
            time.sleep(2 * (attempt + 1))
    raise ApiError(f"API 호출 실패: {last}")


def call_all(op: str, params: dict, max_pages: int = 200) -> list[dict]:
    """모든 페이지를 가져옴. 큰 numOfRows 가 거부되면 100으로 낮춰 재시도."""
    rows = _rows_ok.get(op) or load_settings()["num_of_rows"]
    try:
        items, total = call(op, params, page=1, rows=rows)
    except (QuotaError, KeyError_):
        raise
    except ApiError:
        if rows <= 100:
            raise
        rows = 100
        items, total = call(op, params, page=1, rows=rows)
    _rows_ok[op] = rows
    out, page = list(items), 2
    while len(out) < total and items and page <= max_pages:
        items, total = call(op, params, page=page, rows=rows)
        out.extend(items)
        page += 1
        time.sleep(0.1)
    return out


def raw_call(op: str, params: dict) -> str:
    """API 점검용: 원본 응답 텍스트."""
    s = load_settings()
    q = {"serviceKey": s["service_key"], "type": "json", "pageNo": 1, "numOfRows": 5}
    q.update({k: v for k, v in params.items() if v not in (None, "")})
    resp = requests.get(OPERATIONS[op], params=q, timeout=30)
    return f"HTTP {resp.status_code}\n{resp.text[:5000]}"
