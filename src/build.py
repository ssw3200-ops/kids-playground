"""오늘 놀이터 가도 될까? — 데이터 만들기

매시간 (GitHub Actions)
  1) 에어코리아 시도별 실시간 측정정보 (전국)         → 지금 미세먼지·초미세먼지·오존
  2) 에어코리아 대기질 예보통보 (PM10·PM25)           → 오늘·내일 지역 예보
  3) 기상청 단기예보 (측정소 근처 격자)               → 시간별 기온·강수·바람·하늘  (새 발표 때만)
  4) 기상청 생활기상지수 자외선 (시·도)               → 자외선                        (새 발표 때만)
매주
  5) 에어코리아 측정소정보                            → 측정소 좌표 (= '우리 동네' 기준점)
  6) 행정안전부 전국어린이놀이시설정보                → 주변 실외 놀이터·실내 놀이시설

결과 (site/ → gh-pages 브랜치로 배포, 커밋 기록 안 쌓임)
  site/now.enc.json            지금 정보 (암호화)
  site/places/<시도>.enc.json  놀이시설 (암호화)
  site/meta.json               업데이트 시각 (비암호화)
  site/status.json             소스별 결과·오류 (비암호화)

사용: python src/build.py --prev prev --out site          (DATA_GO_KR_KEY, PAGE_PASSWORD 필요)
      python src/build.py --out site --sample             (키 없이 샘플 데이터)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from lock import decrypt, encrypt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)

AIR = "https://apis.data.go.kr/B552584/ArpltnInforInqireSvc"
STN_URL = "https://apis.data.go.kr/B552584/MsrstnInfoInqireSvc/getMsrstnList"
VF_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
UV_URLS = ["https://apis.data.go.kr/1360000/LivingWthrIdxServiceV5/getUVIdxV5",
           "https://apis.data.go.kr/1360000/LivingWthrIdxServiceV4/getUVIdxV4"]
PF_URL = "https://apis.data.go.kr/1741000/pfc3/getPfctInfo3"

STATION_DAYS = 7   # 측정소 목록 다시 받는 주기
PLACE_DAYS = 7     # 놀이시설 다시 받는 주기

REGIONS = {
    "서울": ("서울",), "부산": ("부산",), "대구": ("대구",), "인천": ("인천",), "광주": ("광주",),
    "대전": ("대전",), "울산": ("울산",), "세종": ("세종",), "경기": ("경기",), "강원": ("강원",),
    "충북": ("충청북도", "충북"), "충남": ("충청남도", "충남"), "전북": ("전라북도", "전북", "전북특별자치도"),
    "전남": ("전라남도", "전남"), "경북": ("경상북도", "경북"), "경남": ("경상남도", "경남"), "제주": ("제주",),
}
CODES = {
    "서울": "seoul", "부산": "busan", "대구": "daegu", "인천": "incheon", "광주": "gwangju", "대전": "daejeon",
    "울산": "ulsan", "세종": "sejong", "경기": "gyeonggi", "강원": "gangwon", "충북": "chungbuk", "충남": "chungnam",
    "전북": "jeonbuk", "전남": "jeonnam", "경북": "gyeongbuk", "경남": "gyeongnam", "제주": "jeju",
}
# 자외선 지역코드 (행정구역코드). 강원·전북은 특별자치도 새 코드 → 옛 코드 순으로 시도
UV_AREA = {
    "서울": ["1100000000"], "부산": ["2600000000"], "대구": ["2700000000"], "인천": ["2800000000"],
    "광주": ["2900000000"], "대전": ["3000000000"], "울산": ["3100000000"], "세종": ["3611000000", "3600000000"],
    "경기": ["4100000000"], "강원": ["5100000000", "4200000000"], "충북": ["4300000000"], "충남": ["4400000000"],
    "전북": ["5200000000", "4500000000"], "전남": ["4600000000"], "경북": ["4700000000"], "경남": ["4800000000"],
    "제주": ["5000000000"],
}

status: dict = {"sources": {}, "errors": [], "reused": []}


def region_of(text: str) -> str:
    first = (text or "").strip().split(" ")[0]
    for short, keys in REGIONS.items():
        if any(first.startswith(k) for k in keys):
            return short
    return ""


def err(where: str, e) -> None:
    msg = f"{where}: {e}"
    print("!!", msg)
    status["errors"].append(msg[:300])


# ---------------------------------------------------------------- API 호출 (JSON/XML 둘 다)
def _find_items(obj) -> list[dict]:
    """응답 JSON 어디에 있든 레코드 목록(dict 리스트)을 찾아서 돌려줌"""
    if isinstance(obj, dict):
        for k in ("item", "items", "list", "row", "data"):
            v = obj.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
            if isinstance(v, dict) and k == "item":
                return [v]
        for v in obj.values():
            got = _find_items(v)
            if got:
                return got
    elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return obj
    return []


def _find_key(obj, names: tuple[str, ...]):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in names and not isinstance(v, (dict, list)):
                return v
        for v in obj.values():
            got = _find_key(v, names)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_key(v, names)
            if got is not None:
                return got
    return None


def call(url: str, params: dict, tries: int = 3, timeout: int = 40) -> tuple[list[dict], int | None]:
    """레코드 목록과 totalCount 를 돌려줌. 오류 코드면 예외."""
    last: Exception | None = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            txt = r.text.strip()
            if txt.startswith("{") or txt.startswith("["):
                js = r.json()
                code = _find_key(js, ("resultCode",))
                if code is not None and str(code) not in ("00", "0", "0000", "INFO-000", "NORMAL_SERVICE"):
                    if str(code) == "03":  # NODATA_ERROR
                        return [], 0
                    raise RuntimeError(f"resultCode={code} {_find_key(js, ('resultMsg',)) or ''}")
                total = _find_key(js, ("totalCount", "totCnt", "totalCnt", "total"))
                return _find_items(js), int(total) if str(total or "").isdigit() else None
            root = ET.fromstring(r.content)
            code = root.findtext(".//resultCode") or root.findtext(".//returnReasonCode") or "00"
            if code == "03":
                return [], 0
            if code not in ("00", "0000", "0"):
                msg = root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg") or ""
                raise RuntimeError(f"resultCode={code} {msg}".strip())
            its = [{c.tag: (c.text or "").strip() for c in it} for it in root.iter("item")]
            tot = root.findtext(".//totalCount")
            return its, int(tot) if tot and tot.isdigit() else None
        except ET.ParseError:
            last = RuntimeError(f"응답이 XML/JSON 아님: {r.text[:150]!r}")
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


def fnum(v) -> float | None:
    try:
        f = float(str(v).strip())
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def latlon(a, b) -> tuple[float, float] | None:
    """두 값 중 한국 위도 범위인 것을 위도로 (기관마다 위경도 필드가 뒤바뀐 경우가 있음)"""
    x, y = fnum(a), fnum(b)
    if x is None or y is None:
        return None
    for lat, lon in ((x, y), (y, x)):
        if 32.5 < lat < 39.5 and 123.5 < lon < 132.5:
            return round(lat, 5), round(lon, 5)
    return None


# ---------------------------------------------------------------- 기상청 격자 변환 (LCC)
def to_grid(lat: float, lon: float) -> tuple[int, int]:
    RE, GRID, SLAT1, SLAT2, OLON, OLAT, XO, YO = 6371.00877, 5.0, 30.0, 60.0, 126.0, 38.0, 43, 136
    D = math.pi / 180
    re_ = RE / GRID
    s1, s2, olon, olat = SLAT1 * D, SLAT2 * D, OLON * D, OLAT * D
    sn = math.log(math.cos(s1) / math.cos(s2)) / math.log(math.tan(math.pi / 4 + s2 / 2) / math.tan(math.pi / 4 + s1 / 2))
    sf = math.tan(math.pi / 4 + s1 / 2) ** sn * math.cos(s1) / sn
    ro = re_ * sf / math.tan(math.pi / 4 + olat / 2) ** sn
    ra = re_ * sf / math.tan(math.pi / 4 + lat * D / 2) ** sn
    th = lon * D - olon
    if th > math.pi:
        th -= 2 * math.pi
    if th < -math.pi:
        th += 2 * math.pi
    th *= sn
    return int(math.floor(ra * math.sin(th) + XO + 0.5)), int(math.floor(ro - ra * math.cos(th) + YO + 0.5))


# ---------------------------------------------------------------- 이전 결과 읽기
def load_prev(prev: Path | None, password: str) -> tuple[dict, dict]:
    """이전 meta.json 과 복호화한 now 데이터. 비밀번호가 바뀌었으면 now 는 빈 dict"""
    meta, now = {}, {}
    if not prev or not prev.exists():
        return meta, now
    try:
        meta = json.loads((prev / "meta.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    try:
        now = decrypt(json.loads((prev / "now.enc.json").read_text(encoding="utf-8")), password)
    except Exception as e:  # noqa: BLE001
        if (prev / "now.enc.json").exists():
            err("이전 데이터 복호화 (비밀번호 변경?)", e)
    return meta, now


def age_days(iso: str | None) -> float:
    if not iso:
        return 999
    try:
        return (NOW - datetime.fromisoformat(iso)).total_seconds() / 86400
    except ValueError:
        return 999


# ---------------------------------------------------------------- 1) 측정소
def fetch_stations(key: str) -> list[dict]:
    its, total = call(STN_URL, {"serviceKey": key, "returnType": "json", "numOfRows": 1000, "pageNo": 1})
    out = []
    for it in its:
        ll = latlon(it.get("dmX"), it.get("dmY"))
        name = (it.get("stationName") or "").strip()
        if not ll or not name:
            continue
        out.append({"n": name, "a": (it.get("addr") or "").strip(), "lat": ll[0], "lon": ll[1]})
    status["sources"]["stations"] = {"total": total, "got": len(out), "fields": sorted(its[0].keys()) if its else []}
    if len(out) < 100:
        raise RuntimeError(f"측정소가 너무 적음 ({len(out)})")
    return out


# ---------------------------------------------------------------- 2) 실시간 대기
def fetch_air(key: str) -> dict[str, dict]:
    its, total = call(f"{AIR}/getCtprvnRltmMesureDnsty", {
        "serviceKey": key, "returnType": "json", "numOfRows": 1000, "pageNo": 1, "sidoName": "전국", "ver": "1.3"})
    out = {}
    for it in its:
        name = (it.get("stationName") or "").strip()
        if not name:
            continue
        out[f'{it.get("sidoName", "")}|{name}'] = {
            "sido": it.get("sidoName", ""),
            "pm10": fnum(it.get("pm10Value")), "pm25": fnum(it.get("pm25Value")), "o3": fnum(it.get("o3Value")),
            "t": it.get("dataTime", ""),
        }
    status["sources"]["air"] = {"total": total, "got": len(out), "fields": sorted(its[0].keys()) if its else [],
                                "dataTime": its[0].get("dataTime") if its else None}
    if len(out) < 100:
        raise RuntimeError(f"측정값이 너무 적음 ({len(out)})")
    return out


# ---------------------------------------------------------------- 3) 대기질 예보
GRADE = {"좋음": 1, "보통": 2, "나쁨": 3, "매우나쁨": 4}


def fetch_forecast(key: str) -> dict:
    """{'2026-11-19': {'세종': {'pm10':2,'pm25':2}, ...}, ...}"""
    out: dict = {}
    for code in ("PM10", "PM25"):
        best: dict[str, str] = {}  # informData → 가장 최근 발표 dataTime
        rows: dict[str, str] = {}
        for day in (NOW.date(), NOW.date() - timedelta(days=1)):
            try:
                its, _ = call(f"{AIR}/getMinuDustFrcstDspth", {
                    "serviceKey": key, "returnType": "json", "numOfRows": 100, "pageNo": 1,
                    "searchDate": day.isoformat(), "InformCode": code})
            except Exception as e:  # noqa: BLE001
                err(f"예보 {code} {day}", e)
                continue
            for it in its:
                if (it.get("informCode") or code) != code:
                    continue
                d, t, g = it.get("informData", ""), it.get("dataTime", ""), it.get("informGrade", "")
                if d and g and t >= best.get(d, ""):
                    best[d], rows[d] = t, g
            if rows:
                break
        for d, g in rows.items():
            for part in g.split(","):
                if ":" not in part:
                    continue
                reg, grade = (x.strip() for x in part.split(":", 1))
                if grade in GRADE:
                    out.setdefault(d, {}).setdefault(reg, {})[code.lower()] = GRADE[grade]
    status["sources"]["forecast"] = {"days": sorted(out.keys()), "regions": len(next(iter(out.values()), {}))}
    return out


# ---------------------------------------------------------------- 4) 단기예보
def latest_base() -> tuple[str, str]:
    """지금 받을 수 있는 가장 최근 단기예보 발표 (발표 후 15분 여유)"""
    t = NOW - timedelta(minutes=15)
    for d in (0, 1):
        day = t - timedelta(days=d)
        for h in (23, 20, 17, 14, 11, 8, 5, 2):
            cand = day.replace(hour=h, minute=0, second=0, microsecond=0)
            if cand <= t:
                return cand.strftime("%Y%m%d"), f"{h:02d}00"
    raise RuntimeError("발표시각 계산 실패")


CATS = {"TMP": "T", "POP": "P", "PTY": "Y", "WSD": "W", "SKY": "S"}
HOURS = 60  # 발표 뒤 60시간 보관


def fetch_grid(key: str, base: tuple[str, str], nx: int, ny: int) -> dict:
    its, _ = call(VF_URL, {"serviceKey": key, "dataType": "JSON", "numOfRows": 1000, "pageNo": 1,
                           "base_date": base[0], "base_time": base[1], "nx": nx, "ny": ny})
    start = datetime.strptime(base[0] + base[1], "%Y%m%d%H%M").replace(tzinfo=KST) + timedelta(hours=1)
    arr = {v: [None] * HOURS for v in CATS.values()}
    for it in its:
        c = CATS.get(it.get("category"))
        if not c:
            continue
        try:
            t = datetime.strptime(str(it["fcstDate"]) + str(it["fcstTime"]), "%Y%m%d%H%M").replace(tzinfo=KST)
        except (KeyError, ValueError):
            continue
        i = int((t - start).total_seconds() // 3600)
        if 0 <= i < HOURS:
            v = fnum(it.get("fcstValue"))
            if v is not None:
                arr[c][i] = round(v * 10) if c == "W" else int(round(v))  # 바람은 0.1m/s 단위 정수
    if all(v is None for v in arr["T"]):
        raise RuntimeError("기온 없음")
    return arr


def fetch_weather(key: str, grids: list[tuple[int, int]], prev_wx: dict) -> dict:
    base = latest_base()
    bkey = base[0] + base[1]
    start = (datetime.strptime(bkey, "%Y%m%d%H%M") + timedelta(hours=1)).strftime("%Y%m%d%H")
    old = prev_wx.get("g", {}) if prev_wx.get("base") == bkey else {}
    todo = [g for g in grids if f"{g[0]},{g[1]}" not in old]
    got = dict(old)
    fails = 0

    def one(g):
        try:
            return g, fetch_grid(key, base, *g)
        except Exception as e:  # noqa: BLE001
            return g, e

    if todo:
        with ThreadPoolExecutor(8) as ex:
            for g, res in ex.map(one, todo):
                if isinstance(res, Exception):
                    fails += 1
                    if fails <= 5:
                        err(f"단기예보 {g}", res)
                else:
                    got[f"{g[0]},{g[1]}"] = res
    status["sources"]["weather"] = {"base": bkey, "grids": len(grids), "called": len(todo), "fails": fails, "have": len(got)}
    # 새 발표를 거의 못 받았으면 이전 발표 유지
    if prev_wx.get("g") and prev_wx.get("base") != bkey and len(got) < len(grids) * 0.5:
        status["reused"].append(f"weather (새 발표 {bkey} 수집 {len(got)}/{len(grids)})")
        return prev_wx
    if not got:
        raise RuntimeError("단기예보 0건")
    return {"base": bkey, "start": start, "g": got}


# ---------------------------------------------------------------- 5) 자외선
def uv_times() -> list[str]:
    """최근 발표 후보 (V5는 3시간마다, V4는 06·18시) — 최신부터"""
    t = NOW - timedelta(minutes=40)
    out = []
    for back in range(0, 25, 3):
        x = t - timedelta(hours=back)
        out.append(x.strftime("%Y%m%d") + f"{x.hour - x.hour % 3:02d}")
    return list(dict.fromkeys(out))


def uv_one(key: str, url: str, code: str, tm: str) -> list | None:
    its, _ = call(url, {"serviceKey": key, "dataType": "JSON", "numOfRows": 10, "pageNo": 1,
                        "areaNo": code, "time": tm}, tries=2)
    if not its:
        return None
    vals = sorted((int(k[1:]), int(fnum(v))) for k, v in its[0].items()
                  if re.fullmatch(r"h\d+", k) and fnum(v) is not None)
    return vals or None


def fetch_uv(key: str, prev_uv: dict) -> dict:
    # 세종으로 지금 받을 수 있는 발표시각·주소를 먼저 찾음
    found = None
    for tm in uv_times():
        if prev_uv.get("time") == tm and prev_uv.get("r"):
            status["sources"]["uv"] = {"time": tm, "reused": True}
            return prev_uv
        for url in UV_URLS:
            try:
                if uv_one(key, url, UV_AREA["세종"][0], tm):
                    found = (url, tm)
                    break
            except Exception as e:  # noqa: BLE001
                err(f"자외선 확인 {url.rsplit('/', 1)[-1]} {tm}", e)
        if found:
            break
    if not found:
        status["sources"]["uv"] = {"found": False}
        return prev_uv or {}
    url, tm = found
    out = {}
    for reg, codes in UV_AREA.items():
        for code in codes:
            try:
                v = uv_one(key, url, code, tm)
            except Exception as e:  # noqa: BLE001
                err(f"자외선 {reg} {code}", e)
                continue
            if v:
                out[reg] = v
                break
    status["sources"]["uv"] = {"time": tm, "api": url.rsplit("/", 1)[-1], "regions": len(out)}
    return {"time": tm, "r": out} if out else (prev_uv or {})


# ---------------------------------------------------------------- 6) 놀이시설
SKIP_PLACE = ("어린이집", "유치원", "학교", "특수학교", "아동복지", "보육", "의료", "병원", "종교", "교회", "사찰")
INDOOR_PLACE = ("식품접객", "대규모점포", "놀이제공영업", "키즈카페", "휴게음식", "일반음식", "제과", "실내", "점포", "영업소")
PARK_PLACE = ("도시공원", "공원", "자연휴양림", "하천", "공공", "도서관", "박물관", "주민센터", "아동센터")
APT_PLACE = ("주택단지", "아파트", "공동주택")


def kind_of(it: dict) -> str | None:
    """p 공원·공공 놀이터 / a 아파트 단지 / i 실내 놀이시설 / o 기타 실외 / None 제외"""
    vals = " ".join(str(v) for k, v in it.items() if isinstance(v, str) and re.search(r"(Nm|Cd|Se|Yn)$", k))
    place = str(it.get("instlPlaceCdNm") or "")
    io = " ".join(str(v) for k, v in it.items() if re.search(r"(idrodr|inout|Idrodr)", k, re.I))
    if any(w in place for w in SKIP_PLACE):
        return None
    if "실내" in io or any(w in place for w in INDOOR_PLACE):
        return "i"
    if any(w in place for w in APT_PLACE):
        return "a"
    if any(w in place for w in PARK_PLACE):
        return "p"
    if "실내" in vals and "실외" not in vals:
        return "i"
    return "o"


def operating(it: dict) -> bool:
    v = str(it.get("operYnCdNm") or it.get("operYn") or "")
    return not any(w in v for w in ("미운영", "폐쇄", "철거", "N"))


def fetch_places(key: str) -> dict[str, list]:
    size = 200
    its0, total = call(PF_URL, {"serviceKey": key, "pageIndex": 1, "recordCountPerPage": size})
    if not its0:
        raise RuntimeError("놀이시설 0건")
    pages = min(1000, math.ceil((total or size * 1000) / size))
    rows = list(its0)
    fails = 0

    def one(p):
        try:
            return call(PF_URL, {"serviceKey": key, "pageIndex": p, "recordCountPerPage": size})[0]
        except Exception as e:  # noqa: BLE001
            return e

    with ThreadPoolExecutor(6) as ex:
        for res in ex.map(one, range(2, pages + 1)):
            if isinstance(res, Exception):
                fails += 1
                if fails <= 5:
                    err("놀이시설 페이지", res)
            else:
                rows += res
    st = {"total": total, "pages": pages, "got": len(rows), "fails": fails, "fields": sorted(its0[0].keys()),
          "sample": {k: v for k, v in its0[0].items() if not re.search(r"(tel|Telno|mng|Mng|nm$)", k)},
          "place_names": {}, "kinds": {}, "skipped": {"closed": 0, "xy": 0, "type": 0, "region": 0}}
    if fails > pages * 0.1:
        raise RuntimeError(f"놀이시설 페이지 실패 {fails}/{pages}")

    out: dict[str, list] = {r: [] for r in REGIONS}
    seen = set()
    for it in rows:
        pid = it.get("pfctSn") or (str(it.get("pfctNm")) + str(it.get("ronaAddr")))
        if pid in seen:
            continue
        seen.add(pid)
        pn = str(it.get("instlPlaceCdNm") or "?")
        st["place_names"][pn] = st["place_names"].get(pn, 0) + 1
        if not operating(it):
            st["skipped"]["closed"] += 1
            continue
        k = kind_of(it)
        if not k:
            st["skipped"]["type"] += 1
            continue
        ll = latlon(it.get("latCrtsVl"), it.get("lotCrtsVl")) or latlon(it.get("lat"), it.get("lot"))
        if not ll:
            st["skipped"]["xy"] += 1
            continue
        addr = str(it.get("ronaAddr") or it.get("lotnoAddr") or it.get("addr") or "").strip()
        reg = region_of(addr)
        if not reg:
            st["skipped"]["region"] += 1
            continue
        name = str(it.get("pfctNm") or "놀이시설").strip()
        out[reg].append([name, addr, ll[0], ll[1], k, pn if pn != "?" else ""])
        st["kinds"][k] = st["kinds"].get(k, 0) + 1
    st["place_names"] = dict(sorted(st["place_names"].items(), key=lambda x: -x[1])[:40])
    status["sources"]["places"] = st
    if sum(len(v) for v in out.values()) < 1000:
        raise RuntimeError("놀이시설 가공 결과가 너무 적음")
    return out


# ---------------------------------------------------------------- 샘플
def sample_data() -> tuple[list, dict, dict, dict, dict, dict]:
    rnd = random.Random(7)
    base_pts = [("세종", "아름동", 36.5113, 127.2454), ("세종", "고운동", 36.5160, 127.2350), ("세종", "보람동", 36.4790, 127.2890),
                ("세종", "조치원읍", 36.6010, 127.2980), ("대전", "둔산동", 36.3510, 127.3850), ("대전", "노은동", 36.3740, 127.3190),
                ("서울", "중구", 37.5640, 126.9750), ("서울", "강남구", 37.5175, 127.0473), ("경기", "수원 인계동", 37.2640, 127.0310)]
    stations = [{"n": n, "a": f"{s} {n}", "lat": la, "lon": lo, "sido": s} for s, n, la, lo in base_pts]
    air = {f"{s['sido']}|{s['n']}": {"sido": s["sido"], "pm10": rnd.choice([22, 38, 55, 92]), "pm25": rnd.choice([9, 18, 29, 41]),
                                     "o3": 0.021, "t": NOW.strftime("%Y-%m-%d %H:00")} for s in stations}
    air["세종|고운동"].update(pm10=34, pm25=17)
    fc = {}
    for d in range(2):
        day = (NOW + timedelta(days=d)).date().isoformat()
        fc[day] = {r: {"pm10": 2, "pm25": 2 if d == 0 else 3} for r in
                   ["서울", "세종", "대전", "충남", "충북", "경기남부", "경기북부", "인천", "부산"]}
    b = latest_base()
    bkey = b[0] + b[1]
    start_dt = datetime.strptime(bkey, "%Y%m%d%H%M").replace(tzinfo=KST) + timedelta(hours=1)
    g = {}
    for s in stations:
        nx, ny = to_grid(s["lat"], s["lon"])
        arr = {v: [] for v in CATS.values()}
        for i in range(HOURS):
            h = (start_dt + timedelta(hours=i)).hour
            t = 4 + 7 * math.sin((h - 9) / 24 * 2 * math.pi) + rnd.uniform(-1, 1)
            arr["T"].append(round(t))
            rain = 1 if 16 <= h <= 18 and i > 20 else 0
            arr["P"].append(70 if rain else rnd.choice([0, 10, 20, 30]))
            arr["Y"].append(rain)
            arr["W"].append(rnd.choice([12, 21, 34, 48]))
            arr["S"].append(rnd.choice([1, 3, 4]))
        g[f"{nx},{ny}"] = arr
    wx = {"base": bkey, "start": start_dt.strftime("%Y%m%d%H"), "g": g}
    uv = {"time": uv_times()[0], "r": {r: [(0, 2), (3, 3), (6, 1), (9, 0), (12, 0), (15, 0), (18, 1), (21, 3), (24, 2)]
                                   for r in ("세종", "대전", "서울", "경기")}}
    places: dict[str, list] = {r: [] for r in REGIONS}
    kinds = ["p", "p", "a", "a", "a", "i", "o"]
    for s in stations:
        for j in range(25):
            k = rnd.choice(kinds)
            nm = {"p": "어린이공원 놀이터", "a": "아파트 놀이터", "i": "키즈카페", "o": "놀이시설"}[k]
            places[s["sido"]].append([f"{s['n']} 샘플 {nm} {j + 1}", f"{s['sido']} 샘플로 {j + 1}",
                                      round(s["lat"] + rnd.uniform(-0.02, 0.02), 5), round(s["lon"] + rnd.uniform(-0.02, 0.02), 5),
                                      k, {"p": "도시공원", "a": "주택단지", "i": "식품접객업소", "o": "기타"}[k]])
    return stations, air, fc, wx, uv, places


# ---------------------------------------------------------------- 조립
def fc_region(sido: str, lat: float, lon: float) -> str:
    if sido == "경기":
        return "경기북부" if lat >= 37.62 else "경기남부"
    if sido == "강원":
        return "영동" if lon >= 128.55 else "영서"
    return sido


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prev", default="")
    ap.add_argument("--out", default="site")
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--force-places", action="store_true")
    a = ap.parse_args()

    out = ROOT / a.out
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT / "docs", out)
    (out / "places").mkdir(exist_ok=True)

    password = os.environ.get("PAGE_PASSWORD", "").strip() or ("1234" if a.sample else "")
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not password:
        print("PAGE_PASSWORD 가 없어요")
        return 1
    prev = Path(a.prev) if a.prev else None
    pmeta, pnow = load_prev(prev, password)
    meta = {"updated": NOW.strftime("%Y-%m-%d %H:%M"), "sample": a.sample,
            "stations_at": pmeta.get("stations_at"), "places_at": pmeta.get("places_at"), "regions": {}}

    if a.sample:
        stations, air, fc, wx, uv, places = sample_data()
        meta["stations_at"] = meta["places_at"] = NOW.isoformat(timespec="minutes")
    else:
        if not key:
            print("DATA_GO_KR_KEY 가 없어요")
            return 1
        # 측정소
        stations = pnow.get("stations_raw") or []
        if not stations or age_days(pmeta.get("stations_at")) > STATION_DAYS:
            try:
                stations = fetch_stations(key)
                meta["stations_at"] = NOW.isoformat(timespec="minutes")
            except Exception as e:  # noqa: BLE001
                err("측정소", e)
                if not stations:
                    raise
                status["reused"].append("stations")
        # 실시간 대기
        try:
            air = fetch_air(key)
        except Exception as e:  # noqa: BLE001
            err("실시간 대기", e)
            air = pnow.get("air_raw") or {}
            status["reused"].append("air")
        try:
            fc = fetch_forecast(key) or pnow.get("fc", {})
        except Exception as e:  # noqa: BLE001
            err("대기 예보", e)
            fc = pnow.get("fc", {})
        places = None
        need_places = a.force_places or age_days(pmeta.get("places_at")) > PLACE_DAYS or not pnow
        if not need_places and prev:
            # 이전 놀이시설 파일이 지금 비밀번호로 열리는지 확인 (비밀번호 바뀌면 다시 받음)
            try:
                f = next((prev / "places").glob("*.enc.json"))
                decrypt(json.loads(f.read_text(encoding="utf-8")), password)
            except Exception:  # noqa: BLE001
                need_places = True
        if need_places:
            try:
                places = fetch_places(key)
                meta["places_at"] = NOW.isoformat(timespec="minutes")
            except Exception as e:  # noqa: BLE001
                err("놀이시설", e)

    # 측정소 ↔ 실시간값 연결 (이름이 같은 측정소가 다른 시도에 있을 수 있어 주소로 시도 판별)
    by_name: dict[str, list] = {}
    for k, v in air.items():
        by_name.setdefault(k.split("|", 1)[1], []).append(v)
    st_out, grids = [], {}
    for s in stations:
        cands = by_name.get(s["n"], [])
        sido = s.get("sido") or region_of(s.get("a", ""))
        v = next((c for c in cands if c["sido"] == sido), cands[0] if len(cands) == 1 else None)
        if not sido and v:
            sido = v["sido"]
        if not sido:
            continue
        nx, ny = to_grid(s["lat"], s["lon"])
        grids[(nx, ny)] = 1
        st_out.append([s["n"], sido, s["lat"], s["lon"], v["pm10"] if v else None, v["pm25"] if v else None,
                       v["o3"] if v else None, v["t"] if v else "", f"{nx},{ny}", fc_region(sido, s["lat"], s["lon"])])
    st_out.sort(key=lambda r: (list(REGIONS).index(r[1]) if r[1] in REGIONS else 99, r[0]))
    status["stations_linked"] = sum(1 for r in st_out if r[4] is not None or r[5] is not None)

    if not a.sample:
        try:
            wx = fetch_weather(key, sorted(grids), pnow.get("wx", {}))
        except Exception as e:  # noqa: BLE001
            err("단기예보", e)
            wx = pnow.get("wx", {})
        try:
            uv = fetch_uv(key, pnow.get("uv", {}))
        except Exception as e:  # noqa: BLE001
            err("자외선", e)
            uv = pnow.get("uv", {})

    now_obj = {"updated": meta["updated"], "st": st_out, "fc": fc, "wx": wx, "uv": uv,
               "stations_raw": stations, "air_raw": air}
    (out / "now.enc.json").write_text(json.dumps(encrypt(now_obj, password)), encoding="utf-8")

    # 놀이시설: 새로 받았으면 암호화해서 쓰고, 아니면 이전 파일 그대로 복사
    if places is not None:
        for reg, rows in places.items():
            rows.sort(key=lambda r: (r[2], r[3]))
            (out / "places" / f"{CODES[reg]}.enc.json").write_text(
                json.dumps(encrypt({"region": reg, "p": rows}, password)), encoding="utf-8")
            meta["regions"][reg] = {"code": CODES[reg], "n": len(rows),
                                    "lat": round(sum(r[2] for r in rows) / len(rows), 4) if rows else None,
                                    "lon": round(sum(r[3] for r in rows) / len(rows), 4) if rows else None}
    elif prev and (prev / "places").exists():
        for f in (prev / "places").glob("*.enc.json"):
            shutil.copy(f, out / "places" / f.name)
        meta["regions"] = pmeta.get("regions", {})
        status["reused"].append("places")
    else:
        meta["regions"] = {}

    meta["weather_base"] = (wx or {}).get("base")
    meta["air_time"] = next((r[7] for r in st_out if r[7]), "")
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    status["updated"] = meta["updated"]
    (out / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in status.items() if k != "sources"}, ensure_ascii=False))
    print({k: {kk: vv for kk, vv in v.items() if kk not in ("fields", "sample", "place_names")} for k, v in status["sources"].items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
