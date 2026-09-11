from __future__ import annotations

import json
import os
from pathlib import Path
import time

from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import ArcGIS, Nominatim
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
FILE_PATH = BASE_DIR / "sales.xlsx"
CACHE_PATH = BASE_DIR / "geocode_cache.json"
OUTPUT_HTML = BASE_DIR / "sales_map.html"
SHEET_NAME = "1.전체매출누계"


def normalize_text(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def load_cache() -> dict[str, list[float | None]]:
    if not CACHE_PATH.exists():
        return {}
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict[str, list[float | None]]) -> None:
    temp_path = CACHE_PATH.with_suffix(".tmp")
    for attempt in range(5):
        try:
            with temp_path.open("w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, CACHE_PATH)
            return
        except (PermissionError, OSError):
            if attempt == 4:
                raise
            time.sleep(0.5)


def is_failed_cache_entry(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) >= 2
        and value[0] is None
        and value[1] is None
    )


def build_address_candidates(address: str) -> list[str]:
    normalized = normalize_text(address)
    if not normalized:
        return []

    candidates: list[str] = []
    seen: set[str] = set()
    variants = [
        normalized,
        f"{normalized}, 대한민국",
        normalized.replace("대한민국", "").strip(),
    ]
    for variant in variants:
        if variant and variant not in seen:
            candidates.append(variant)
            seen.add(variant)
    return candidates


def geocode_address(arcgis_geocode, nominatim_geocode, address: str) -> tuple[float, float] | None:
    for candidate in build_address_candidates(address):
        for attempt in range(2):
            location = arcgis_geocode(candidate, exactly_one=True, timeout=15)
            if location is not None:
                return float(location.latitude), float(location.longitude)
            if attempt < 1:
                time.sleep(1.5)

        for attempt in range(2):
            location = nominatim_geocode(
                candidate,
                country_codes="kr",
                language="ko",
                exactly_one=True,
                timeout=10,
            )
            if location is not None:
                return float(location.latitude), float(location.longitude)
            if attempt < 1:
                time.sleep(2.0)
    return None


def geocode_unique_addresses(addresses: list[str]) -> dict[str, list[float | None]]:
    cache = load_cache()
    missing = [
        addr
        for addr in addresses
        if addr and (addr not in cache or is_failed_cache_entry(cache.get(addr)))
    ]

    if not missing:
        return cache

    arcgis_geolocator = ArcGIS()
    arcgis_geocode = RateLimiter(
        arcgis_geolocator.geocode,
        min_delay_seconds=1.0,
        swallow_exceptions=True,
        return_value_on_exception=None,
    )

    nominatim_geolocator = Nominatim(user_agent="optipharm-korea-sales-map-html/1.0")
    nominatim_geocode = RateLimiter(
        nominatim_geolocator.geocode,
        min_delay_seconds=2.0,
        swallow_exceptions=True,
        return_value_on_exception=None,
    )

    print(f"\n좌표가 없는 주소 {len(missing):,}개를 변환합니다.")
    for index, address in enumerate(missing, start=1):
        coords = geocode_address(arcgis_geocode, nominatim_geocode, address)
        if coords is None:
            cache.pop(address, None)
            result_text = "실패"
        else:
            lat, lon = coords
            cache[address] = [lat, lon]
            result_text = f"{lat:.5f}, {lon:.5f}"

        print(f"[{index:>3}/{len(missing)}] {result_text} | {address}")
        if index % 20 == 0:
            save_cache(cache)

    save_cache(cache)
    return cache


def load_data() -> tuple[pd.DataFrame, int, int]:
    if not FILE_PATH.exists():
        raise FileNotFoundError(f"'{FILE_PATH.name}' 파일을 찾을 수 없습니다.")

    raw = pd.read_excel(
        FILE_PATH,
        sheet_name=SHEET_NAME,
        usecols=["부서명", "매출액", "주소", "거래처명"],
    )

    source_rows = len(raw)
    raw = raw.copy()
    raw["부서명"] = raw["부서명"].map(normalize_text)
    raw["주소"] = raw["주소"].map(normalize_text)
    raw["거래처명"] = raw["거래처명"].map(normalize_text)
    raw["매출액"] = pd.to_numeric(raw["매출액"], errors="coerce").fillna(0)
    raw = raw[(raw["부서명"] != "") & (raw["주소"] != "")]

    grouped = (
        raw.groupby(["부서명", "주소", "거래처명"], as_index=False, dropna=False)["매출액"]
        .sum()
    )

    unique_addresses = sorted(grouped["주소"].unique().tolist())
    cache = geocode_unique_addresses(unique_addresses)

    grouped["lat"] = grouped["주소"].map(lambda address: cache.get(address, [None, None])[0])
    grouped["lon"] = grouped["주소"].map(lambda address: cache.get(address, [None, None])[1])

    failed_count = grouped.loc[grouped[["lat", "lon"]].isna().any(axis=1), "주소"].nunique()
    grouped = grouped.dropna(subset=["lat", "lon"]).copy()

    return grouped, source_rows, failed_count


def generate_html() -> None:
    data, source_rows, failed_count = load_data()
    departments = sorted(data["부서명"].dropna().unique().tolist())

    # Leaflet 지도에서 바로 사용할 수 있도록 JSON 리스트로 변환
    records = []
    for _, row in data.iterrows():
        records.append({
            "dept": row["부서명"],
            "addr": row["주소"],
            "client": row["거래처명"],
            "sales": float(row["매출액"]),
            "lat": float(row["lat"]),
            "lon": float(row["lon"])
        })

    # [수정 부분] 백슬래시 오류를 피하기 위해 부서 버튼 HTML을 미리 변수로 생성합니다.
    button_elements = []
    for dept in departments:
        button_elements.append(
            f'<button class="dept-btn" onclick="filterDept(\'{dept}\', this)">{dept}</button>'
        )
    dept_buttons_html = "".join(button_elements)

    failed_color = "#b45309" if failed_count > 0 else "#475569"

    html_content = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>한국 매출 지도</title>
    <!-- Leaflet CSS & JS -->
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body {{
            margin: 0;
            padding: 0;
            background-color: #f8fafc;
            font-family: "Malgun Gothic", "Apple SD Gothic Neo", NanumGothic, sans-serif;
            color: #334155;
        }}
        .header {{
            padding: 20px 22px 10px;
        }}
        .header h2 {{
            margin: 0 0 6px;
            font-size: 24px;
        }}
        .header p {{
            margin: 0;
            color: #475569;
            font-size: 14px;
        }}
        .dept-selector {{
            padding: 0 18px 10px;
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }}
        .dept-btn {{
            padding: 9px 14px;
            border: 1px solid #cbd5e1;
            border-radius: 9px;
            background-color: white;
            cursor: pointer;
            font-size: 14px;
            transition: all 0.2s;
        }}
        .dept-btn:hover {{
            background-color: #f1f5f9;
        }}
        .dept-btn.active {{
            background-color: #2563eb;
            color: white;
            border-color: #2563eb;
        }}
        .summary-cards {{
            padding: 0 22px 10px;
            display: flex;
            gap: 16px;
            flex-wrap: wrap;
        }}
        .card {{
            font-size: 15px;
        }}
        .card strong {{
            font-size: 18px;
            color: #0f172a;
        }}
        #map {{
            height: 70vh;
            min-height: 520px;
            width: 100%;
        }}
        .footer {{
            padding: 12px 22px 22px;
            color: #475569;
            font-size: 14px;
            display: flex;
            flex-direction: column;
            gap: 4px;
        }}
        .footer .failed {{
            color: {failed_color};
        }}
    </style>
</head>
<body>

    <div class="header">
        <h2>한국 매출 지도</h2>
        <p>부서 버튼을 누르면 해당 부서의 매출 발생 지역만 표시됩니다.</p>
    </div>

    <div class="dept-selector" id="dept-buttons">
        <button class="dept-btn active" onclick="filterDept('전체', this)">전체</button>
        {dept_buttons_html}
    </div>

    <div class="summary-cards" id="summary-cards">
        <div class="card"><strong id="sum-sales">0원</strong> <span>매출액</span></div>
        <div class="card"><strong id="sum-addr">0개</strong> <span>주소</span></div>
        <div class="card"><strong id="sum-client">0개</strong> <span>거래처</span></div>
    </div>

    <div id="map"></div>

    <div class="footer">
        <div>원본 {source_rows:,}행 · 지도 표시 {len(data):,}개 집계 지점 · 부서 {len(departments)}개</div>
        <div class="failed">좌표 변환 실패 주소: {failed_count:,}개</div>
    </div>

    <script>
        const rawData = {json.dumps(records, ensure_ascii=False)};
        
        // 지도 초기화
        const map = L.map('map').setView([36.5, 127.8], 7);
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '&copy; OpenStreetMap contributors'
        }}).addTo(map);

        let markerLayerGroup = L.layerGroup().addTo(map);

        // 매출액에 따른 컬러 계열 함수 (파란색 -> 빨간색)
        function getColor(value, maxVal) {{
            const ratio = Math.min(Math.abs(value) / maxVal, 1);
            if (ratio > 0.8) return '#d90429';
            if (ratio > 0.5) return '#f77f00';
            if (ratio > 0.2) return '#fcbf49';
            return '#2a9d8f';
        }}

        function filterDept(deptName, btnElement) {{
            // 버튼 스타일 활성화 변경
            document.querySelectorAll('.dept-btn').forEach(btn => btn.classList.remove('active'));
            if (btnElement) btnElement.classList.add('active');

            markerLayerGroup.clearLayers();

            const filtered = deptName === '전체' 
                ? rawData 
                : rawData.filter(d => d.dept === deptName);

            // 1. 요약 카드 업데이트
            const totalSales = filtered.reduce((acc, cur) => acc + cur.sales, 0);
            const uniqueAddrs = new Set(filtered.map(d => d.addr)).size;
            const uniqueClients = new Set(filtered.map(d => d.client)).size;

            document.getElementById('sum-sales').innerText = totalSales.toLocaleString('ko-KR') + '원';
            document.getElementById('sum-addr').innerText = uniqueAddrs.toLocaleString('ko-KR') + '개';
            document.getElementById('sum-client').innerText = uniqueClients.toLocaleString('ko-KR') + '개';

            if (filtered.length === 0) return;

            // 2. 최대 매출액 구하기 (크기/색상 스케일링용)
            const maxSales = Math.max(...filtered.map(d => Math.abs(d.sales)), 1);

            // 3. 마커 및 바운드 설정
            const bounds = [];
            filtered.forEach(d => {{
                bounds.push([d.lat, d.lon]);

                // 점 크기 계산 (원 반지름 4px ~ 24px)
                const radius = 4 + (Math.abs(d.sales) / maxSales) * 20;
                const color = getColor(d.sales, maxSales);

                const circle = L.circleMarker([d.lat, d.lon], {{
                    radius: radius,
                    fillColor: color,
                    color: '#ffffff',
                    weight: 1,
                    opacity: 1,
                    fillOpacity: 0.8
                }});

                const popupContent = `
                    <div style="font-size: 13px; line-height: 1.5;">
                        <strong>${{d.client}}</strong><br/>
                        <span style="color: #64748b;">부서:</span> ${{d.dept}}<br/>
                        <span style="color: #64748b;">주소:</span> ${{d.addr}}<br/>
                        <span style="color: #64748b;">매출액:</span> <strong>${{d.sales.toLocaleString('ko-KR')}}원</strong>
                    </div>
                `;
                circle.bindPopup(popupContent);
                markerLayerGroup.addLayer(circle);
            }});

            // 4. 지도 화면 위치를 데이터에 맞게 자동 조절
            if (bounds.length > 0) {{
                map.fitBounds(bounds, {{ padding: [30, 30], maxZoom: 11 }});
            }}
        }}

        // 초기 화면 '전체' 부서 로드
        filterDept('전체', document.querySelector('.dept-btn.active'));
    </script>
</body>
</html>
"""

    with OUTPUT_HTML.open("w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"\n[완료] HTML 파일이 생성되었습니다: {OUTPUT_HTML.name}")
    print("이제 생성된 HTML 파일을 웹브라우저로 더블 클릭하여 여시면 됩니다.")


if __name__ == "__main__":
    generate_html()