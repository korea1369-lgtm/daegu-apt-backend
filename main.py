from functools import lru_cache
import os
import re
import sqlite3
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
import numpy as np
import pandas as pd

# 1. 사용할 전국 통합 SQLite DB 파일 경로 지정
DB_FILE = "apt_data_master.db"


# 2. 단지명 캐시 로딩 (검색 자동완성용)
def clean_apt_name(raw_name):
  if not raw_name:
    return ""
  return re.sub(r"\[.*?\]", "", raw_name).strip()


def load_all_apt_names():
  if not os.path.exists(DB_FILE):
    return []
  conn = sqlite3.connect(DB_FILE)
  c = conn.cursor()
  c.execute("SELECT DISTINCT apt_name FROM apt_trades ORDER BY apt_name ASC")
  rows = c.fetchall()
  names = []
  for r in rows:
    n = clean_apt_name(str(r[0]))
    if n and not re.match(r"^[\d\-\(\)]+$", n) and n not in names:
      names.append(n)
  conn.close()
  return names


CACHED_APT_NAMES = load_all_apt_names()

APT_COMPARE_RANKINGS = {
    "청라힐스자이": [
        "남산자이하늘채",
        "남산롯데캐슬센트럴스카이",
        "더샵디어엘로",
        "대신센트럴자이",
        "힐스테이트대구역",
        "수성범어W",
    ],
    "더샵디어엘로": [
        "동대구역화성파크드림",
        "청라힐스자이",
        "동대구더샵센트럴시티",
        "이안센트럴D",
        "신천센트럴자이",
        "힐스테이트대구역",
        "남산자이하늘채",
    ],
    "수성범어W": [
        "힐스테이트범어",
        "두산위브더제니스(대구 수성)",
        "e편한세상범어",
        "범어SKVIEW",
        "더샵디어엘로",
        "남산자이하늘채",
    ],
    "힐스테이트대구역": [
        "대구역오페라W",
        "힐스테이트도원센트럴",
        "대구역유림노르웨이숲",
        "청라힐스자이",
        "더샵디어엘로",
        "남산자이하늘채",
    ],
}
DEFAULT_RANK = [
    "수성범어W",
    "두산위브더제니스(대구 수성)",
    "더샵디어엘로",
    "청라힐스자이",
    "힐스테이트대구역",
    "남산자이하늘채",
]

LAWD_CD_MAP = {
    "대구전체": [
        "27110",
        "27140",
        "27170",
        "27200",
        "27230",
        "27260",
        "27290",
        "27710",
    ],
    "중구": ["27110"],
    "동구": ["27140"],
    "서구": ["27170"],
    "남구": ["27200"],
    "북구": ["27230"],
    "수성구": ["27260"],
    "달서구": ["27290"],
    "달성군": ["27710"],
}

app = FastAPI()


@app.get("/api/search-apt")
def search_apt(q: str = Query("")):
  query_str = q.strip().lower()
  if not query_str:
    return CACHED_APT_NAMES[:20]
  q_compact = query_str.replace(" ", "")
  keywords = query_str.split()
  matched = []
  for name in CACHED_APT_NAMES:
    name_lower = name.lower()
    if q_compact in name_lower.replace(" ", "") or all(
        k in name_lower for k in keywords
    ):
      matched.append(name)
      if len(matched) >= 20:
        break
  return matched


@lru_cache(maxsize=128)
def query_chart_from_db(pure_name: str, months: int, area_type: str):
  conn = sqlite3.connect(DB_FILE)
  max_date_row = conn.execute(
      "SELECT MAX(deal_date) FROM apt_trades"
  ).fetchone()
  if not max_date_row or not max_date_row[0]:
    conn.close()
    return None, None, []

  end_date = pd.to_datetime(max_date_row[0])
  start_date = end_date - pd.DateOffset(months=months)
  start_str = start_date.strftime("%Y-%m-%d")

  area_cond = ""
  params = [pure_name, start_str]
  if area_type == "84":
    area_cond = "AND exclu_use_ar >= 83.0 AND exclu_use_ar <= 85.99"
  elif area_type == "59":
    area_cond = "AND exclu_use_ar >= 58.0 AND exclu_use_ar <= 60.99"

  query = f"""
        SELECT deal_date, deal_amount, exclu_use_ar, floor
        FROM apt_trades
        WHERE apt_name = ?
          AND deal_date >= ?
          {area_cond}
        ORDER BY deal_date ASC
    """
  df = pd.read_sql_query(query, conn, params=params)
  conn.close()
  return start_str, max_date_row[0], df.to_dict("records")


@app.get("/api/chart-data")
def get_chart_data(
    apt_name: str = Query(...),
    months: int = Query(12),
    area_type: str = Query("84"),
):
  pure_name = clean_apt_name(apt_name)
  start_str, max_date, raw_records = query_chart_from_db(
      pure_name, months, area_type
  )

  conn = sqlite3.connect(DB_FILE)
  cur = conn.cursor()
  cur.execute(
      "SELECT built_year, built_str, units_str, type_info FROM apt_meta_master"
      " WHERE apt_name = ?",
      (pure_name,),
  )
  meta_row = cur.fetchone()

  if meta_row and "집계중" not in meta_row[2] and "확인중" not in meta_row[2]:
    byear, bstr, ustr, tinfo = meta_row
  else:
    cur.execute(
        "SELECT MIN(deal_date) FROM apt_trades WHERE apt_name = ?",
        (pure_name,),
    )
    min_date_row = cur.fetchone()
    min_date = min_date_row[0] if min_date_row else None
    byear = int(min_date[:4]) if min_date else 2020
    age = 2026 - byear + 1
    bstr = f"{byear}년 ({age}년차)"
    ustr = "단지 세대수 집계중"
    cur.execute(
        "SELECT ROUND(exclu_use_ar), COUNT(*) FROM apt_trades WHERE apt_name ="
        " ? GROUP BY ROUND(exclu_use_ar) ORDER BY COUNT(*) DESC LIMIT 4",
        (pure_name,),
    )
    areas = cur.fetchall()
    tinfo = (
        " / ".join([f"{int(a[0])}㎡({a[1]}건)" for a in areas])
        if areas
        else "전용 정보 확인중"
    )
  conn.close()

  top10_list = APT_COMPARE_RANKINGS.get(pure_name, DEFAULT_RANK)

  if not raw_records:
    return {
        "result": "empty",
        "dates": [],
        "prices": [],
        "ma": [],
        "upper": [],
        "lower": [],
        "details": [],
        "start_date": start_str,
        "built_date": f"{byear}-01-01",
        "stats": {
            "built": bstr,
            "units": ustr,
            "type_info": tinfo,
            "trade_count": 0,
            "max_price": "-",
            "max_info": "기간 내 거래 없음",
            "avg_price": "-",
            "latest_ma": "-",
            "dispersion": "-",
        },
        "top10": top10_list,
        "pure_name": pure_name,
    }

  df = pd.DataFrame(raw_records)
  if area_type == "all":
    df["price"] = (df["deal_amount"] / (df["exclu_use_ar"] / 3.30578)).round(1)
    unit_suffix = "만원/평"
  else:
    df["price"] = (df["deal_amount"] / 10000.0).round(3)
    unit_suffix = "억"

  mean_val = df["price"].mean()
  std_val = df["price"].std()
  if pd.notnull(mean_val) and mean_val > 0 and pd.notnull(std_val):
    cv = (std_val / mean_val) * 100.0
    if cv <= 3.5:
      disp_badge = f"{round(cv, 1)}% (매우 안정)"
    elif cv <= 6.5:
      disp_badge = f"{round(cv, 1)}% (안정)"
    elif cv <= 10.0:
      disp_badge = f"{round(cv, 1)}% (보통)"
    else:
      disp_badge = f"{round(cv, 1)}% (편차 큼)"
  else:
    disp_badge = "-"

  prices = df["price"].tolist()
  ma_list, upper_list, lower_list = [], [], []
  for i in range(len(prices)):
    window = prices[max(0, i - 19) : i + 1]
    m = float(np.mean(window))
    s = float(np.std(window))
    ma_list.append(round(m, 2))
    upper_list.append(round(m + (s * 2), 2))
    lower_list.append(round(m - (s * 2), 2))

  details = [
      {
          "excluUseAr": round(float(r["exclu_use_ar"]), 2),
          "floor": int(r["floor"]) if pd.notnull(r["floor"]) else "-",
      }
      for _, r in df.iterrows()
  ]
  max_idx = df["price"].idxmax()
  max_row = df.loc[max_idx]

  stats = {
      "built": bstr,
      "units": ustr,
      "type_info": tinfo,
      "trade_count": len(df),
      "max_price": f"{max_row['price']}{unit_suffix}",
      "max_info": (
          f"계약일: {max_row['deal_date']} /"
          f" {int(max_row['floor']) if pd.notnull(max_row['floor']) else '-'}층"
          f" ({round(float(max_row['exclu_use_ar']),1)}㎡)"
      ),
      "avg_price": f"{round(mean_val, 2)}{unit_suffix}",
      "latest_ma": f"{ma_list[-1]}{unit_suffix}" if ma_list else "-",
      "dispersion": disp_badge,
  }

  return {
      "result": "ok",
      "dates": df["deal_date"].tolist(),
      "prices": prices,
      "ma": ma_list,
      "upper": upper_list,
      "lower": lower_list,
      "start_date": start_str,
      "built_date": f"{byear}-01-01",
      "details": details,
      "stats": stats,
      "top10": top10_list,
      "pure_name": pure_name,
  }


@app.get("/api/rankings")
def get_rankings(
    year: int = Query(2026),
    rank_type: str = Query("price_max"),
    regions: str = Query("대구전체"),
):
  conn = sqlite3.connect(DB_FILE)
  lawd_codes = []
  if "대구전체" in regions:
    lawd_codes = LAWD_CD_MAP["대구전체"]
  else:
    for r in regions.split(","):
      r_clean = r.strip()
      if r_clean in LAWD_CD_MAP:
        lawd_codes.extend(LAWD_CD_MAP[r_clean])
  lawd_codes = list(set(lawd_codes)) if lawd_codes else LAWD_CD_MAP["대구전체"]

  placeholders = ",".join(["?"] * len(lawd_codes))
  where_extra = ""

  if rank_type == "price_max":
    order_col, metric_name = "COALESCE(s.max_price, 0) DESC", "단지 최고가"
  elif rank_type == "84_max":
    where_extra = "AND s.max_84_price > 0"
    order_col, metric_name = "s.max_84_price DESC", "국평(84) 최고가"
  elif rank_type == "84_avg":
    where_extra = "AND s.avg_84_price > 0"
    order_col, metric_name = "s.avg_84_price DESC", "국평(84) 평균가"
  elif rank_type == "59_max":
    where_extra = "AND s.max_59_price > 0"
    order_col, metric_name = "s.max_59_price DESC", "전용 59 최고가"
  elif rank_type == "59_avg":
    where_extra = "AND s.avg_59_price > 0"
    order_col, metric_name = "s.avg_59_price DESC", "전용 59 평균가"
  elif rank_type == "trade_cnt":
    order_col, metric_name = "s.total_trade_cnt DESC", "연간 거래량"
  elif rank_type == "pyeong_avg":
    order_col, metric_name = "COALESCE(s.avg_pyeong, 0) DESC", "평균 평당가"
  else:
    order_col, metric_name = "COALESCE(s.max_price, 0) DESC", "단지 최고가"

  if "84" in rank_type:
    units_select = "COALESCE(m.units_84_str, '-')"
  elif "59" in rank_type:
    units_select = "COALESCE(m.units_59_str, '-')"
  else:
    units_select = "COALESCE(m.units_str, '-')"

  query = f"""
        SELECT 
            s.apt_name, s.lawd_5, 
            s.total_trade_cnt, s.trade_cnt_84, s.trade_cnt_59,
            COALESCE(s.max_price, 0) as max_price, COALESCE(s.avg_price, 0) as avg_price,
            COALESCE(s.max_84_price, 0) as max_84_price, COALESCE(s.avg_84_price, 0) as avg_84_price,
            COALESCE(s.max_59_price, 0) as max_59_price, COALESCE(s.avg_59_price, 0) as avg_59_price,
            s.max_p_date, s.max_p_area, s.max_p_pyeong_est, s.max_p_floor,
            COALESCE(s.dispersion_cv, 0) as dispersion_cv,
            COALESCE(m.built_str, '-') as built_str, 
            {units_select} as units_str,
            COALESCE(m.type_info, '-') as type_info
        FROM apt_rank_yearly_summary s
        LEFT JOIN apt_meta_master m ON s.apt_name = m.apt_name
        WHERE s.deal_year = ? AND s.lawd_5 IN ({placeholders}) {where_extra}
        ORDER BY {order_col} LIMIT 50
    """
  df = pd.read_sql_query(query, conn, params=[year] + lawd_codes)
  conn.close()

  lawd_to_gu = {
      "27110": "중구",
      "27140": "동구",
      "27170": "서구",
      "27200": "남구",
      "27230": "북구",
      "27260": "수성구",
      "27290": "달서구",
      "27710": "달성군",
  }
  results = []

  for idx, r in df.iterrows():
    gu = lawd_to_gu.get(str(r["lawd_5"]), "대구")

    if "84" in rank_type:
      display_trade_cnt = int(r["trade_cnt_84"])
    elif "59" in rank_type:
      display_trade_cnt = int(r["trade_cnt_59"])
    else:
      display_trade_cnt = int(r["total_trade_cnt"])

    tooltip_info = ""
    if rank_type == "price_max" and r["max_p_date"]:
      tooltip_info = (
          f"계약일: {r['max_p_date']} | {r['max_price']}억 | 약"
          f" {r['max_p_pyeong_est']}평형(전용 {r['max_p_area']}㎡) |"
          f" {r['max_p_floor']}층"
      )

    if rank_type == "price_max":
      metric_val = f"{r['max_price']} 억"
    elif rank_type == "84_max":
      metric_val = f"{r['max_84_price']} 억"
    elif rank_type == "84_avg":
      metric_val = f"{r['avg_84_price']} 억"
    elif rank_type == "59_max":
      metric_val = f"{r['max_59_price']} 억"
    elif rank_type == "59_avg":
      metric_val = f"{r['avg_59_price']} 억"
    elif rank_type == "trade_cnt":
      metric_val = f"{int(r['total_trade_cnt']):,} 건"
    elif rank_type == "pyeong_avg":
      metric_val = f"{r['avg_pyeong']:,.1f} 만원/평"
    else:
      metric_val = f"{r['max_price']} 억"

    cv_val = float(r["dispersion_cv"])
    if cv_val <= 0 or r["total_trade_cnt"] < 2:
      disp_badge = "-"
    elif cv_val <= 3.5:
      disp_badge = f"{cv_val:.1f}% (매우 안정)"
    elif cv_val <= 6.5:
      disp_badge = f"{cv_val:.1f}% (안정)"
    elif cv_val <= 10.0:
      disp_badge = f"{cv_val:.1f}% (보통)"
    else:
      disp_badge = f"{cv_val:.1f}% (편차 큼)"

    results.append({
        "rank": idx + 1,
        "apt_name": r["apt_name"],
        "region": f"대구 {gu}",
        "metric_name": metric_name,
        "metric_val": metric_val,
        "tooltip_info": tooltip_info,
        "dispersion": disp_badge,
        "trade_cnt": display_trade_cnt,
        "built_str": r["built_str"],
        "units_str": r["units_str"],
        "type_info": r["type_info"],
    })
  return results


UI_HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <title>대구 아파트 실거래가 기술적 분석실</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
  <style>
    :root { --primary: #2563eb; --bg: #f8fafc; --card: #ffffff; --border: #e2e8f0; --text: #0f172a; --sub: #64748b; }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 0; background: var(--bg); color: var(--text); }
    .global-nav { background: #0f172a; color: #fff; padding: 0 28px; height: 60px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 10000; box-shadow: 0 4px 12px rgba(0,0,0,0.15); }
    .logo-area { font-size: 18px; font-weight: 800; color: #38bdf8; cursor: pointer; }
    .nav-links { display: flex; gap: 6px; height: 100%; align-items: center; }
    .nav-btn { background: transparent; border: none; color: #94a3b8; font-size: 14px; font-weight: 600; padding: 0 16px; height: 40px; border-radius: 8px; cursor: pointer; transition: all 0.15s; }
    .nav-btn:hover { color: #fff; background: rgba(255,255,255,0.08); }
    .nav-btn.active { color: #38bdf8; background: rgba(56,189,248,0.12); font-weight: 700; }
    .main-wrapper { max-width: 1440px; margin: 24px auto; padding: 0 20px 40px; }
    .page-view { display: none; }
    .page-view.active { display: block; }
    .hero-banner { background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); color: #fff; border-radius: 18px; padding: 40px; margin-bottom: 26px; }
    .hero-banner h1 { font-size: 26px; font-weight: 800; margin-bottom: 10px; }
    .hero-banner p { font-size: 15px; color: #94a3b8; line-height: 1.6; }
    .hub-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; }
    .hub-card { background: #fff; border: 1px solid var(--border); border-radius: 16px; padding: 24px; cursor: pointer; transition: all 0.2s; box-shadow: 0 4px 16px rgba(0,0,0,0.04); }
    .hub-card:hover { transform: translateY(-4px); box-shadow: 0 12px 30px rgba(37,99,235,0.12); border-color: var(--primary); }
    .hub-title { font-size: 19px; font-weight: 700; color: #1e293b; margin-bottom: 8px; }
    .hub-desc { font-size: 13.5px; color: #64748b; line-height: 1.5; margin-bottom: 18px; }
    .hub-action { font-size: 13.5px; font-weight: 700; color: var(--primary); }
    .ranking-header { background: #fff; border: 1px solid var(--border); border-radius: 16px; padding: 22px 24px; margin-bottom: 20px; }
    .year-selection-bar { display: flex; align-items: center; gap: 8px; overflow-x: auto; padding-bottom: 12px; margin-bottom: 16px; border-bottom: 1px solid #f1f5f9; }
    .year-label { font-size: 13.5px; font-weight: 800; color: #0f172a; white-space: nowrap; margin-right: 4px; }
    .btn-year { padding: 6px 14px; border-radius: 8px; font-size: 13px; font-weight: 700; border: 1px solid #cbd5e1; background: #fff; color: #475569; cursor: pointer; white-space: nowrap; }
    .btn-year.active { background: #2563eb; color: #fff; border-color: #2563eb; }
    .rank-tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
    .rank-tab-btn { padding: 8px 16px; border-radius: 9px; font-size: 13px; font-weight: 700; border: 1px solid #cbd5e1; background: #fff; color: #475569; cursor: pointer; }
    .rank-tab-btn.active { background: #0f172a; color: #fff; border-color: #0f172a; }
    .filter-section { border-top: 1px solid #f1f5f9; padding-top: 14px; }
    .region-parent-row { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-bottom: 10px; }
    .region-checkbox-label { display: flex; align-items: center; gap: 6px; font-size: 13.5px; font-weight: 600; color: #1e293b; cursor: pointer; }
    .btn-subregion-toggle { font-size: 12px; font-weight: 700; color: #e11d48; background: #fff1f2; border: 1px solid #fecdd3; padding: 3px 8px; border-radius: 6px; cursor: pointer; }
    .subregion-container { background: #f8fafc; border: 1px solid var(--border); border-radius: 10px; padding: 12px 16px; display: flex; gap: 16px; flex-wrap: wrap; }
    .subregion-item { display: flex; align-items: center; gap: 5px; font-size: 13px; font-weight: 500; color: #334155; cursor: pointer; }
    .ranking-table-card { background: #fff; border: 1px solid var(--border); border-radius: 16px; padding: 22px; position: relative; }
    .ranking-loading-overlay { position: absolute; inset: 0; background: rgba(255, 255, 255, 0.85); backdrop-filter: blur(2px); border-radius: 16px; display: none; flex-direction: column; align-items: center; justify-content: center; z-index: 100; gap: 12px; }
    .loading-hourglass { font-size: 34px; animation: spinHourglass 1.4s infinite ease-in-out; }
    @keyframes spinHourglass { 0% { transform: rotate(0deg); } 50% { transform: rotate(180deg); } 100% { transform: rotate(360deg); } }
    .loading-text { font-size: 14px; font-weight: 700; color: #1e293b; }
    table.rank-table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
    table.rank-table th { background: #f8fafc; padding: 12px 10px; text-align: center; font-weight: 700; color: #475569; border-bottom: 2px solid #e2e8f0; }
    table.rank-table td { padding: 12px 10px; text-align: center; border-bottom: 1px solid #f1f5f9; }
    table.rank-table tr:hover { background-color: #f8fafc; }
    .rank-num { font-weight: 800; font-size: 15px; width: 50px; }
    .rank-num.top1 { color: #e11d48; }
    .rank-num.top2 { color: #f97316; }
    .rank-num.top3 { color: #eab308; }
    .apt-name-click { font-weight: 700; color: #1e293b; cursor: pointer; text-decoration: underline; text-underline-offset: 3px; }
    .apt-name-click:hover { color: var(--primary); }
    .metric-hover-box { position: relative; display: inline-block; cursor: help; }
    .metric-tooltip { visibility: hidden; opacity: 0; position: absolute; bottom: 125%; left: 50%; transform: translateX(-50%); background-color: #0f172a; color: #f8fafc; padding: 8px 12px; border-radius: 8px; font-size: 12px; font-weight: 600; white-space: nowrap; box-shadow: 0 8px 24px rgba(0,0,0,0.3); z-index: 5000; transition: opacity 0.15s ease-in-out; pointer-events: none; }
    .metric-tooltip::after { content: ""; position: absolute; top: 100%; left: 50%; margin-left: -5px; border-width: 5px; border-style: solid; border-color: #0f172a transparent transparent transparent; }
    .metric-hover-box:hover .metric-tooltip { visibility: visible; opacity: 1; }
    .help-tooltip-trigger { display: inline-flex; align-items: center; justify-content: center; width: 15px; height: 15px; border-radius: 50%; background: #94a3b8; color: #fff; font-size: 10.5px; font-weight: 700; cursor: help; margin-left: 4px; position: relative; vertical-align: middle; }
    .help-tooltip-trigger:hover { background: var(--primary); }
    .help-tooltip-box { display: none; position: absolute; right: 0; top: 120%; width: 420px; max-height: 500px; overflow-y: auto; background: #0f172a; color: #f8fafc; border-radius: 12px; padding: 16px 18px; font-size: 12px; line-height: 1.55; z-index: 3000; box-shadow: 0 12px 32px rgba(0,0,0,0.35); text-align: left; }
    .help-tooltip-trigger:hover .help-tooltip-box { display: block; }
    .tooltip-title { font-weight: 800; color: #38bdf8; font-size: 13.5px; margin-bottom: 6px; }
    .tooltip-def { background: #1e293b; padding: 8px 10px; border-radius: 8px; border-left: 3px solid #38bdf8; font-size: 12px; margin-bottom: 10px; color: #e2e8f0; }
    .container { max-width: 1440px; margin: 0 auto; background: var(--card); padding: 28px; border-radius: 18px; box-shadow: 0 6px 24px rgba(0,0,0,0.06); }
    .header-area { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; border-bottom: 2px solid #f1f5f9; padding-bottom: 16px; flex-wrap: wrap; gap: 12px; }
    h2 { font-size: 22px; font-weight: 700; color: #1e293b; }
    .area-filter-bar { display: inline-flex; background: #e2e8f0; padding: 3px; border-radius: 10px; gap: 4px; }
    .filter-btn { padding: 7px 15px; border-radius: 8px; font-size: 13.5px; font-weight: 600; border: none; background: transparent; color: var(--sub); cursor: pointer; }
    .filter-btn.active { background: #fff; color: var(--primary); box-shadow: 0 2px 6px rgba(0,0,0,0.08); }
    .selector-container { background: #f8fafc; border: 1px solid var(--border); border-radius: 14px; padding: 16px; margin-bottom: 18px; }
    .slot-config-row { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; font-size: 14px; font-weight: 600; color: #334155; }
    .search-inputs-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
    .search-slot-wrapper { position: relative; width: 100%; }
    .select-item { display: flex; align-items: center; gap: 10px; background: #fff; padding: 8px 12px; border-radius: 10px; border: 1px solid var(--border); transition: border-color 0.2s; }
    .select-item:focus-within { border-color: var(--primary); box-shadow: 0 0 0 2px rgba(37,99,235,0.12); }
    .color-pill { width: 14px; height: 14px; border-radius: 50%; flex-shrink: 0; }
    input.search-input { width: 100%; border: none; outline: none; font-size: 13.5px; font-weight: 600; background: transparent; color: #0f172a; }
    .autocomplete-dropdown { position: absolute; top: calc(100% + 4px); left: 0; right: 0; background: #ffffff; border: 1px solid var(--border); border-radius: 10px; max-height: 250px; overflow-y: auto; z-index: 1000; box-shadow: 0 8px 24px rgba(0,0,0,0.12); display: none; }
    .ac-item { padding: 9px 14px; font-size: 13px; font-weight: 500; cursor: pointer; border-bottom: 1px solid #f8fafc; display: flex; align-items: center; justify-content: space-between; }
    .ac-item:hover { background-color: #f1f5f9; color: var(--primary); font-weight: 600; }
    .ac-item.ac-none { background-color: #fff1f2; color: #e11d48; font-weight: 700; border-bottom: 1px solid #fecdd3; }
    .ac-item.ac-disabled { opacity: 0.45; cursor: not-allowed; background-color: #f8fafc; }
    .toolbar { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 18px; padding-bottom: 14px; border-bottom: 1px solid var(--border); }
    .tool-group { display: flex; align-items: center; gap: 10px; }
    select.period-select { padding: 7px 12px; font-size: 13.5px; border-radius: 8px; border: 1px solid #cbd5e1; background: #fff; font-weight: 600; }
    .btn-toggle { padding: 6px 14px; font-size: 13px; font-weight: 600; border-radius: 8px; border: 1px solid var(--border); background: #fff; cursor: pointer; transition: all 0.2s; }
    .btn-toggle.active { background: #0284c7; color: #fff; border-color: #0284c7; }
    .btn-reset-zoom { padding: 6px 12px; font-size: 12.5px; font-weight: 700; border-radius: 8px; border: 1px solid #cbd5e1; background: #f8fafc; color: #334155; cursor: pointer; }
    .btn-reset-zoom:hover { background: #e2e8f0; color: #0f172a; }
    .dashboard-body { display: flex; gap: 20px; align-items: flex-start; margin-bottom: 28px; }
    .chart-section { flex: 1 1 60%; min-width: 0; display: flex; flex-direction: column; gap: 14px; }
    .chart-box { position: relative; height: 580px; background: #fff; border: 1px solid var(--border); border-radius: 14px; padding: 16px; cursor: grab; }
    .chart-box:active { cursor: grabbing; }
    .chart-loading-overlay { position: absolute; inset: 0; background: rgba(255, 255, 255, 0.82); backdrop-filter: blur(2px); border-radius: 14px; display: none; flex-direction: column; align-items: center; justify-content: center; z-index: 50; gap: 12px; }
    .loading-hourglass { font-size: 34px; animation: spinHourglass 1.4s infinite ease-in-out; }
    @keyframes spinHourglass { 0% { transform: rotate(0deg); } 50% { transform: rotate(180deg); } 100% { transform: rotate(360deg); } }
    .loading-text { font-size: 14px; font-weight: 700; color: #1e293b; }
    .chart-guide-card { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 14px 18px; }
    .guide-title { font-size: 13px; font-weight: 700; color: #334155; margin-bottom: 8px; display: flex; align-items: center; justify-content: space-between; }
    .guide-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; font-size: 12px; }
    .guide-item { background: #fff; padding: 10px 12px; border-radius: 8px; border: 1px solid #e2e8f0; line-height: 1.45; }
    .guide-item strong { display: block; font-size: 12.5px; margin-bottom: 4px; }
    .right-side-panel { flex: 1 1 40%; min-width: 360px; display: flex; flex-direction: column; gap: 16px; }
    .compare-panel { background: #ffffff; border: 1px solid var(--border); border-radius: 14px; padding: 18px; }
    .compare-title { font-size: 15.5px; font-weight: 700; color: #1e293b; margin-bottom: 12px; display: flex; align-items: center; justify-content: space-between; }
    table.compare-table { width: 100%; border-collapse: collapse; font-size: 13px; }
    table.compare-table th, table.compare-table td { padding: 9px 6px; text-align: center; border-bottom: 1px solid #f1f5f9; }
    table.compare-table th.metric-col { text-align: left; font-weight: 700; color: #475569; background: #f8fafc; width: 34%; padding-left: 8px; }
    .type-info-cell { font-size: 11.5px; line-height: 1.4; color: #475569; word-break: keep-all; }
    .top10-panel { background: #f8fafc; border: 1px solid var(--border); border-radius: 14px; padding: 18px; }
    .top10-title { font-size: 14.5px; font-weight: 700; color: #1e293b; margin-bottom: 10px; }
    .top10-tabs { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
    .top10-tab-btn { padding: 5px 10px; font-size: 12px; font-weight: 600; border-radius: 6px; border: 1px solid #cbd5e1; background: #fff; cursor: pointer; }
    .top10-tab-btn.active { background: #0f172a; color: #fff; border-color: #0f172a; }
    .top10-list { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .top10-item { font-size: 12px; color: #334155; padding: 6px 8px; background: #fff; border: 1px solid #e2e8f0; border-radius: 6px; cursor: pointer; display: flex; align-items: center; justify-content: space-between; }
  </style>
</head>
<body>
  <nav class="global-nav">
    <div class="logo-area" onclick="navigateTo('home')">🏢 <span>TECH REALTY INSIGHT</span></div>
    <div class="nav-links">
      <button class="nav-btn active" id="nav-home" onclick="navigateTo('home')">홈 (허브)</button>
      <button class="nav-btn" id="nav-rank" onclick="navigateTo('rank')">아파트 랭킹</button>
      <button class="nav-btn" id="nav-vs" onclick="navigateTo('vs')">아파트 vs 아파트</button>
      <button class="nav-btn" style="opacity: 0.45; cursor: not-allowed;">부동산 매크로</button>
      <button class="nav-btn" style="opacity: 0.45; cursor: not-allowed;">지역 vs 지역</button>
    </div>
  </nav>

  <div class="main-wrapper">
    <div class="page-view active" id="page-home">
      <div class="hero-banner">
        <h1>📊 부동산 데이터 테크니컬 분석 플랫폼</h1>
        <p>수십만 건의 국토부 실거래가 원천 데이터와 기술적 지표를 통해 단지의 가치와 랭킹을 확인합니다.</p>
      </div>
      <div class="hub-grid">
        <div class="hub-card" onclick="navigateTo('rank')">
          <div style="font-size: 12px; font-weight: 700; color: #7e22ce; margin-bottom: 8px;">스마트 랭킹</div>
          <div class="hub-title">🏆 아파트 랭킹 (2010~2026)</div>
          <div class="hub-desc">단지 최고가, 국평(84), 전용 59 최고가·평균가, 분산도 및 거래량 랭킹을 즉시 확인합니다.</div>
          <div class="hub-action">단지별 랭킹 확인하기 ➔</div>
        </div>
        <div class="hub-card" onclick="navigateTo('vs')">
          <div style="font-size: 12px; font-weight: 700; color: #1d4ed8; margin-bottom: 8px;">핵심 분석실</div>
          <div class="hub-title">⚔️ 아파트 vs 아파트 비교</div>
          <div class="hub-desc">최대 4개 단지를 한 차트에 올려 실거래 점, 20건 이동평균선, 볼린저 밴드를 정밀 비교합니다.</div>
          <div class="hub-action">비교 분석실 바로가기 ➔</div>
        </div>
      </div>
    </div>

    <div class="page-view" id="page-rank">
      <div class="ranking-header">
        <div style="font-size: 18px; font-weight: 800; color: #0f172a; margin-bottom: 14px;">🏆 실거래가 기반 아파트 종합 랭킹</div>
        <div class="year-selection-bar">
          <span class="year-label">📅 기준 연도:</span>
          <div id="yearButtonsContainer" style="display: flex; gap: 6px;"></div>
        </div>
        <div class="rank-tabs">
          <button class="rank-tab-btn active" onclick="setRankType('price_max', this)">단지 최고가 순위</button>
          <button class="rank-tab-btn" onclick="setRankType('84_max', this)">국평(84) 최고가 순위</button>
          <button class="rank-tab-btn" onclick="setRankType('84_avg', this)">국평(84) 평균가 순위</button>
          <button class="rank-tab-btn" onclick="setRankType('59_max', this)">전용 59 최고가 순위</button>
          <button class="rank-tab-btn" onclick="setRankType('59_avg', this)">전용 59 평균가 순위</button>
          <button class="rank-tab-btn" onclick="setRankType('pyeong_avg', this)">평균 평당가 순위</button>
          <button class="rank-tab-btn" onclick="setRankType('trade_cnt', this)">거래량 순위</button>
        </div>
        <div class="filter-section">
          <div class="region-parent-row">
            <label class="region-checkbox-label"><input type="checkbox" id="chkAllRegions" onchange="toggleAllRegions(this)" checked> 전체</label>
            <div style="display: flex; align-items: center;">
              <label class="region-checkbox-label"><input type="checkbox" class="region-chk" value="대구전체" checked onchange="handleRegionCheck()"> 대구</label>
              <button class="btn-subregion-toggle" onclick="toggleSubregionBox()">세부지역 ▼</button>
            </div>
          </div>
          <div class="subregion-container" id="subregionBox" style="display: flex;">
            <label class="subregion-item"><input type="checkbox" class="gu-chk" value="수성구" checked onchange="handleGuCheck()"> 수성구</label>
            <label class="subregion-item"><input type="checkbox" class="gu-chk" value="중구" checked onchange="handleGuCheck()"> 중구</label>
            <label class="subregion-item"><input type="checkbox" class="gu-chk" value="동구" checked onchange="handleGuCheck()"> 동구</label>
            <label class="subregion-item"><input type="checkbox" class="gu-chk" value="북구" checked onchange="handleGuCheck()"> 북구</label>
            <label class="subregion-item"><input type="checkbox" class="gu-chk" value="달서구" checked onchange="handleGuCheck()"> 달서구</label>
            <label class="subregion-item"><input type="checkbox" class="gu-chk" value="남구" checked onchange="handleGuCheck()"> 남구</label>
            <label class="subregion-item"><input type="checkbox" class="gu-chk" value="서구" checked onchange="handleGuCheck()"> 서구</label>
            <label class="subregion-item"><input type="checkbox" class="gu-chk" value="달성군" checked onchange="handleGuCheck()"> 달성군</label>
          </div>
        </div>
      </div>
      <div class="ranking-table-card">
        <div class="ranking-loading-overlay" id="rankingLoadingOverlay">
          <div class="loading-hourglass">⏳</div>
          <div class="loading-text">실거래 랭킹 빅데이터 로딩 중...</div>
        </div>
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
          <span style="font-size: 15px; font-weight: 700; color: #1e293b;" id="rankTableTitle">2026년 Top 50 랭킹</span>
          <span style="font-size: 12.5px; color: #64748b;">(단지명 클릭 시 [아파트 vs 아파트] 차트 분석으로 즉시 이동)</span>
        </div>
        <div style="overflow-x: auto;">
          <table class="rank-table">
            <thead>
              <tr id="rankTableHeaderRow">
                <th>순위</th>
                <th>단지명</th>
                <th>지역</th>
                <th style="color: #2563eb;" id="rankMetricHeader">기준 지표</th>
                <th>입주 연식</th>
                <th>세대수</th>
                <th>연간 거래량</th>
                <th>
                  <span>가격 분산도</span>
                  <span class="help-tooltip-trigger">?
                    <div class="help-tooltip-box">
                      <div class="tooltip-title">💡 가격 분산도(Price Dispersion)란?</div>
                      <div class="tooltip-def"><strong>정의:</strong> 해당 연도 실거래 평당가의 <strong>변동계수(CV% = 표준편차/평균가)</strong>입니다.</div>
                    </div>
                  </span>
                </th>
              </tr>
            </thead>
            <tbody id="rankTableBody">
              <tr><td colspan="8" style="padding: 30px; color: #94a3b8;">데이터를 불러오는 중입니다...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="page-view" id="page-vs">
      <div class="container">
        <div class="header-area">
          <h2>📊 대구 아파트 실거래가 기술적 분석실 (마스터 클린 버전)</h2>
          <div class="area-filter-bar">
            <button class="filter-btn active" onclick="setAreaFilter('84', this)">전용 84㎡ (83~85)</button>
            <button class="filter-btn" onclick="setAreaFilter('59', this)">전용 59㎡ (58~60)</button>
            <button class="filter-btn" onclick="setAreaFilter('all', this)">전체 평형(평당가)</button>
          </div>
        </div>

        <div class="selector-container">
          <div class="slot-config-row">
            <span>비교 단지 수:</span>
            <select id="slotCountSelect" onchange="updateSlotCount(this.value)">
              <option value="2">2개 단지</option>
              <option value="3" selected>3개 단지</option>
              <option value="4">4개 단지</option>
            </select>
          </div>
          <div class="search-inputs-grid" id="searchInputsContainer"></div>
        </div>

        <div class="toolbar">
          <div class="tool-group">
            <label style="font-size: 13.5px; font-weight: 600;">분석 주기:</label>
            <select id="periodSelect" class="period-select" onchange="fetchAndRender()"></select>
          </div>
          <div class="tool-group">
            <button id="toggleScatterBtn" class="btn-toggle active" onclick="toggleCategory('scatter')">● 실거래가 점</button>
            <button id="toggleSMABtn" class="btn-toggle active" onclick="toggleCategory('sma')">━ 이동평균선</button>
            <button id="toggleBBBtn" class="btn-toggle active" onclick="toggleCategory('bb')">╍ 볼린저 밴드</button>
            <button class="btn-reset-zoom" onclick="resetChartZoom()">🔍 줌 리셋</button>
          </div>
        </div>

        <div class="dashboard-body">
          <div class="chart-section">
            <div class="chart-box">
              <div class="chart-loading-overlay" id="chartLoadingOverlay">
                <div class="loading-hourglass">⏳</div>
                <div class="loading-text">실거래 빅데이터 정밀 분석 중...</div>
              </div>
              <canvas id="aptChart"></canvas>
            </div>
            <div class="chart-guide-card">
              <div class="guide-title">
                <span>📌 상단 레이어 및 인터랙션 조작 안내</span>
                <span style="font-size: 11.5px; color: #059669; font-weight: 600;">✨ 마우스 휠 줌(확대/축소) & 드래그 이동(Pan) 지원</span>
              </div>
              <div class="guide-grid">
                <div class="guide-item guide-scatter"><strong>● 실거래가 점 (ON/OFF)</strong>국토부 실거래 신고 건입니다. 마우스를 올리면 계약일자, 층수, 전용면적 스펙을 확인할 수 있습니다.</div>
                <div class="guide-item guide-sma"><strong>━ 20일 이동평균선 (ON/OFF)</strong>직전 20건 거래의 가격 흐름선입니다. 층별·급매별 왜곡을 지우고 단지의 진짜 시세 추세를 파악합니다.</div>
                <div class="guide-item guide-bb"><strong>╍ 볼린저 밴드 (ON/OFF)</strong>통계적 정상 거래 가격 범위(±2σ)입니다. 밴드 폭이 좁을수록 단지 시세가 촘촘하고 안정적입니다.</div>
                <div class="guide-item guide-zoom"><strong>🔍 Y축 확대 및 화면 이동</strong>차트 위에서 <strong>마우스 휠을 굴리면 Y축(가격)이 정밀 확대</strong>되며, <strong>마우스로 클릭 후 드래그하면 위아래로 이동</strong>합니다.</div>
              </div>
            </div>
          </div>

          <div class="right-side-panel">
            <div class="compare-panel">
              <div class="compare-title"><span>⚖️ 단지 종합 스펙 & 실거래 비교</span></div>
              <div id="compareTableWrapper"></div>
            </div>
            <div class="top10-panel">
              <div class="top10-title">🔥 자주 함께 비교되는 단지 Top 10</div>
              <div class="top10-tabs" id="top10TabsContainer"></div>
              <div class="top10-list" id="top10ListContainer"></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

<script>
  let currentYear = 2026;
  let currentRankType = 'price_max';

  function showRankingLoading(show) {
    const overlay = document.getElementById('rankingLoadingOverlay');
    if (overlay) overlay.style.display = show ? 'flex' : 'none';
  }

  function buildYearButtons() {
    const container = document.getElementById('yearButtonsContainer');
    if (!container) return;
    container.innerHTML = '';
    for (let y = 2026; y >= 2010; y--) {
      const btn = document.createElement('button');
      btn.className = `btn-year ${y === currentYear ? 'active' : ''}`;
      btn.innerText = `${y}년`;
      btn.onclick = () => {
        currentYear = y;
        document.querySelectorAll('.btn-year').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        fetchRankings();
      };
      container.appendChild(btn);
    }
  }

  function navigateTo(pageId) {
    document.querySelectorAll('.page-view').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.nav-btn').forEach(el => el.classList.remove('active'));
    document.getElementById(`page-${pageId}`).classList.add('active');
    const navBtn = document.getElementById(`nav-${pageId}`);
    if (navBtn) navBtn.classList.add('active');

    if (pageId === 'vs') {
      if (!chart) initVsChart();
      else fetchAndRender();
    } else if (pageId === 'rank') {
      fetchRankings();
    }
  }

  function setRankType(type, btn) {
    currentRankType = type;
    document.querySelectorAll('.rank-tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    fetchRankings();
  }

  function toggleSubregionBox() {
    const box = document.getElementById('subregionBox');
    box.style.display = (box.style.display === 'none') ? 'flex' : 'none';
  }

  function toggleAllRegions(master) {
    const checked = master.checked;
    document.querySelectorAll('.region-chk, .gu-chk').forEach(c => c.checked = checked);
    fetchRankings();
  }

  function handleRegionCheck() {
    const deaguAll = document.querySelector('.region-chk[value="대구전체"]').checked;
    document.querySelectorAll('.gu-chk').forEach(c => c.checked = deaguAll);
    fetchRankings();
  }

  function handleGuCheck() {
    const allGus = Array.from(document.querySelectorAll('.gu-chk'));
    const allChecked = allGus.every(c => c.checked);
    document.querySelector('.region-chk[value="대구전체"]').checked = allChecked;
    fetchRankings();
  }

  function getSelectedRegions() {
    const deaguAll = document.querySelector('.region-chk[value="대구전체"]')?.checked;
    if (deaguAll) return "대구전체";
    const checkedGus = Array.from(document.querySelectorAll('.gu-chk:checked')).map(c => c.value);
    return checkedGus.length > 0 ? checkedGus.join(',') : "대구전체";
  }

  async function fetchRankings() {
    showRankingLoading(true);
    const regions = getSelectedRegions();
    document.getElementById('rankTableTitle').innerText = `${currentYear}년 Top 50 랭킹`;

    try {
      const res = await fetch(`/api/rankings?year=${currentYear}&rank_type=${currentRankType}&regions=${encodeURIComponent(regions)}`);
      if (!res.ok) throw new Error('API 응답 에러');
      const list = await res.json();
      const tbody = document.getElementById('rankTableBody');
      
      if (!list || list.length === 0) {
        tbody.innerHTML = `<tr><td colspan="8" style="padding: 30px; color: #94a3b8;">${currentYear}년도 해당 조건의 거래 데이터가 없습니다. (다른 연도를 선택해보세요)</td></tr>`;
        return;
      }
      
      document.getElementById('rankMetricHeader').innerText = list[0].metric_name;

      let html = '';
      list.forEach(item => {
        let rankClass = item.rank === 1 ? 'top1' : (item.rank === 2 ? 'top2' : (item.rank === 3 ? 'top3' : ''));
        let metricContent = `<span style="font-weight: 800; color: #2563eb;">${item.metric_val}</span>`;
        if (currentRankType === 'price_max' && item.tooltip_info) {
          metricContent = `
            <div class="metric-hover-box">
              <span style="font-weight: 800; color: #2563eb; text-decoration: underline dotted;">${item.metric_val}</span>
              <div class="metric-tooltip">📌 ${item.tooltip_info}</div>
            </div>
          `;
        }

        html += `
          <tr>
            <td class="rank-num ${rankClass}">${item.rank}</td>
            <td style="text-align: left;"><span class="apt-name-click" onclick="sendToVsCompare('${item.apt_name}')">${item.apt_name}</span></td>
            <td><span style="background: #f1f5f9; padding: 3px 8px; border-radius: 6px; font-size: 12px; font-weight: 600;">${item.region}</span></td>
            <td>${metricContent}</td>
            <td style="color: #475569;">${item.built_str}</td>
            <td style="color: #475569;">${item.units_str}</td>
            <td style="font-weight: 600;">${item.trade_cnt}건</td>
            <td style="font-weight: 700; color: #0f172a;">${item.dispersion}</td>
          </tr>
        `;
      });
      tbody.innerHTML = html;
    } catch(e) {
      document.getElementById('rankTableBody').innerHTML = '<tr><td colspan="8" style="padding: 30px; color: #ef4444;">랭킹 데이터를 불러오지 못했습니다.</td></tr>';
    } finally {
      showRankingLoading(false);
    }
  }

  function sendToVsCompare(aptName) {
    navigateTo('vs');
    const input1 = document.getElementById('aptInput1');
    if (input1) {
      input1.value = aptName;
      fetchAndRender();
    }
  }

  const slotConfigs = [
    { slot: 1, color: "#2563eb", fill: "rgba(37, 99, 235, 0.08)", defaultVal: "더샵디어엘로" },
    { slot: 2, color: "#f97316", fill: "rgba(249, 115, 22, 0.08)", defaultVal: "청라힐스자이" },
    { slot: 3, color: "#16a34a", fill: "rgba(22, 163, 74, 0.08)", defaultVal: "수성범어W" },
    { slot: 4, color: "#9333ea", fill: "rgba(147, 51, 234, 0.08)", defaultVal: "힐스테이트대구역" }
  ];

  let currentAreaMode = '84';
  let currentSlotCount = 3;
  let visibilityFlags = { scatter: true, sma: true, bb: true };
  let chart = null;
  let globalSlotResults = [];
  let currentActiveTabIdx = 0;

  function showLoading(show) {
    const overlay = document.getElementById('chartLoadingOverlay');
    if (overlay) overlay.style.display = show ? 'flex' : 'none';
  }

  function resetChartZoom() {
    if (chart) chart.resetZoom();
  }

  function buildPeriodOptions() {
    const sel = document.getElementById('periodSelect');
    if (!sel) return;
    sel.innerHTML = '';
    for (let m = 3; m <= 36; m++) {
      const opt = document.createElement('option');
      opt.value = m;
      opt.innerText = (m % 12 === 0) ? `${m}개월 (${m/12}년)` : `${m}개월`;
      if (m === 24) opt.selected = true;
      sel.appendChild(opt);
    }
    for (let y = 4; y <= 20; y++) {
      const opt = document.createElement('option');
      opt.value = y * 12;
      opt.innerText = `${y}년 (${y * 12}개월)`;
      sel.appendChild(opt);
    }
  }

  function setAreaFilter(mode, btn) {
    currentAreaMode = mode;
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    fetchAndRender();
  }

  function updateSlotCount(count) {
    currentSlotCount = parseInt(count);
    renderSearchInputs();
    fetchAndRender();
  }

  function isAptEmpty(val) { return !val || val.trim() === "" || val.includes("없음"); }

  function getSelectedAptNames(excludeSlotIdx) {
    const names = new Set();
    for (let i = 1; i <= currentSlotCount; i++) {
      if (i === excludeSlotIdx) continue;
      const inp = document.getElementById(`aptInput${i}`);
      if (inp && !isAptEmpty(inp.value)) {
        names.add(inp.value.trim().replace(" ", ""));
      }
    }
    return names;
  }

  function closeAllDropdowns(exceptIdx = -1) {
    for (let i = 1; i <= 4; i++) {
      if (i === exceptIdx) continue;
      const dd = document.getElementById(`acDropdown${i}`);
      if (dd) dd.style.display = 'none';
    }
  }

  function renderSearchInputs() {
    const container = document.getElementById('searchInputsContainer');
    if (!container) return;
    container.innerHTML = '';
    for (let i = 0; i < currentSlotCount; i++) {
      const cfg = slotConfigs[i];
      const slotDiv = document.createElement('div');
      slotDiv.className = 'search-slot-wrapper';
      slotDiv.innerHTML = `
        <div class="select-item">
          <span class="color-pill" style="background-color: ${cfg.color};"></span>
          <input class="search-input" id="aptInput${i+1}" value="${cfg.defaultVal}"
                 placeholder="단지명 검색 (예: 힐스테이트)"
                 onfocus="openAutocomplete(${i+1})"
                 oninput="handleSearchInput(${i+1})"
                 onkeydown="handleKeyDown(event, ${i+1})">
        </div>
        <div class="autocomplete-dropdown" id="acDropdown${i+1}"></div>
      `;
      container.appendChild(slotDiv);
    }
  }

  async function handleSearchInput(slotIdx) {
    const input = document.getElementById(`aptInput${slotIdx}`);
    if (input) openAutocomplete(slotIdx, input.value.trim());
  }

  async function openAutocomplete(slotIdx, query = "") {
    closeAllDropdowns(slotIdx);
    const input = document.getElementById(`aptInput${slotIdx}`);
    if (!input) return;
    const q = (query !== "") ? query : input.value.trim();
    const dropdown = document.getElementById(`acDropdown${slotIdx}`);
    if (!dropdown) return;
    const alreadySelected = getSelectedAptNames(slotIdx);

    try {
      const res = await fetch(`/api/search-apt?q=${encodeURIComponent(q)}`);
      const aptList = await res.json();
      let html = `<div class="ac-item ac-none" onmousedown="selectApt(${slotIdx}, '')">🚫 [없음]</div>`;
      aptList.forEach(name => {
        const isDuplicated = alreadySelected.has(name.replace(" ", ""));
        if (isDuplicated) {
          html += `<div class="ac-item ac-disabled"><span>🏢 ${name}</span><span style="font-size:11px; color:#ef4444; font-weight:700;">[선택됨]</span></div>`;
        } else {
          html += `<div class="ac-item" onmousedown="selectApt(${slotIdx}, '${name}')"><span>🏢 ${name}</span><span style="font-size:11px; color:#94a3b8;">선택</span></div>`;
        }
      });
      dropdown.innerHTML = html;
      dropdown.style.display = 'block';
    } catch(e) {}
  }

  function selectApt(slotIdx, name) {
    const input = document.getElementById(`aptInput${slotIdx}`);
    const dropdown = document.getElementById(`acDropdown${slotIdx}`);
    if (!input) return;
    if (name !== "" && getSelectedAptNames(slotIdx).has(name.replace(" ", ""))) {
      alert("이미 다른 슬롯에서 비교 중인 단지입니다.");
      if (dropdown) dropdown.style.display = 'none';
      return;
    }
    input.value = name;
    if (dropdown) dropdown.style.display = 'none';
    fetchAndRender();
  }

  function handleKeyDown(e, slotIdx) {
    if (e.key === 'Enter') {
      const input = document.getElementById(`aptInput${slotIdx}`);
      if (!input) return;
      const val = input.value.trim();
      if (val && getSelectedAptNames(slotIdx).has(val.replace(" ", ""))) {
        alert("이미 다른 슬롯에서 비교 중인 단지입니다.");
        input.value = "";
      }
      closeAllDropdowns();
      fetchAndRender();
    }
  }

  document.addEventListener('click', (e) => {
    let clickedInside = false;
    for (let i = 1; i <= 4; i++) {
      if (document.getElementById(`acDropdown${i}`)?.contains(e.target) || document.getElementById(`aptInput${i}`)?.contains(e.target)) {
        clickedInside = true; break;
      }
    }
    if (!clickedInside) closeAllDropdowns();
  });

  function renderCompareTable(results) {
    const wrapper = document.getElementById('compareTableWrapper');
    if (!wrapper || !results || results.length === 0) return;
    const currentMonths = document.getElementById('periodSelect')?.value || 24;
    const periodDisplayStr = (currentMonths >= 48) ? `${currentMonths/12}년(${currentMonths}개월)` : `${currentMonths}개월`;

    let tradeLabel = "3. 거래량 (전용 84㎡)";
    if (currentAreaMode === '59') tradeLabel = "3. 거래량 (전용 59㎡)";
    else if (currentAreaMode === 'all') tradeLabel = "3. 거래량 (전체)";

    let html = '<table class="compare-table"><thead><tr><th class="metric-col">항목</th>';
    results.forEach(r => html += `<th style="color: ${r.cfg.color}; font-size:13px;">${r.aptName}</th>`);
    html += '</tr></thead><tbody>';

    html += '<tr><th class="metric-col">1. 입주(연식)</th>';
    results.forEach(r => html += `<td style="font-weight:600;">${r.data.stats?.built || '-'}</td>`);
    html += '</tr>';

    html += '<tr><th class="metric-col">2. 세대수</th>';
    results.forEach(r => html += `<td style="font-weight:700; color:#0f172a;">${r.data.stats?.units || '-'}</td>`);
    html += '</tr>';

    html += '<tr><th class="metric-col" style="background:#f1f5f9;">└ 평형 구성</th>';
    results.forEach(r => html += `<td class="type-info-cell">${r.data.stats?.type_info || '-'}</td>`);
    html += '</tr>';

    html += `<tr><th class="metric-col" style="color:#2563eb;">${tradeLabel}</th>`;
    results.forEach(r => html += `<td style="font-weight:700; color:#2563eb;">${r.data.stats?.trade_count || 0}건</td>`);
    html += '</tr>';

    html += '<tr><th class="metric-col">4. 최고가</th>';
    results.forEach(r => html += `<td style="font-weight:700;">${r.data.stats?.max_price || '-'}</td>`);
    html += '</tr>';

    html += `<tr>
      <th class="metric-col">
        <span>5. 누적 평균가</span>
        <span class="help-tooltip-trigger">?
          <div class="help-tooltip-box">
            <div class="tooltip-title">💡 누적 평균가 vs 이동평균선 차이 안내</div>
            <div class="tooltip-def"><strong>누적 평균가:</strong> 선택하신 <strong>${periodDisplayStr} 동안 발생한 모든 실거래가의 단순 산술평균</strong>입니다.</div>
          </div>
        </span>
      </th>`;
    results.forEach(r => html += `<td style="font-weight:600;">${r.data.stats?.avg_price || '-'}</td>`);
    html += '</tr>';

    html += '<tr><th class="metric-col" style="color:#0284c7;">└ 최근 이평시세</th>';
    results.forEach(r => html += `<td style="font-weight:700; color:#0284c7;">${r.data.stats?.latest_ma || '-'}</td>`);
    html += '</tr>';

    html += `<tr>
      <th class="metric-col" style="background:#f8fafc;">
        <span>6. 가격 분산도</span>
        <span class="help-tooltip-trigger">?
          <div class="help-tooltip-box">
            <div class="tooltip-title">💡 가격 분산도(Price Dispersion)란?</div>
            <div class="tooltip-def"><strong>정의:</strong> 같은 단지·평형 내 실거래가의 <strong>변동계수(CV% = 표준편차/평균가)</strong>입니다.</div>
          </div>
        </span>
      </th>`;
    results.forEach(r => html += `<td style="font-weight:700; color:#0f172a;">${r.data.stats?.dispersion || '-'}</td>`);
    html += '</tr></tbody></table>';
    wrapper.innerHTML = html;
  }

  function renderTop10Rankings(results) {
    const tabsContainer = document.getElementById('top10TabsContainer');
    const listContainer = document.getElementById('top10ListContainer');
    if (!tabsContainer || !listContainer || !results || results.length === 0) return;
    tabsContainer.innerHTML = ''; listContainer.innerHTML = '';

    results.forEach((r, idx) => {
      const btn = document.createElement('button');
      btn.className = `top10-tab-btn ${idx === currentActiveTabIdx ? 'active' : ''}`;
      btn.innerText = `단지 ${idx + 1} (${r.aptName})`;
      btn.onclick = () => { currentActiveTabIdx = idx; renderTop10Rankings(globalSlotResults); };
      tabsContainer.appendChild(btn);
    });

    const activeItem = results[currentActiveTabIdx];
    if (!activeItem || !activeItem.data.top10) return;

    activeItem.data.top10.forEach((name, rank) => {
      const div = document.createElement('div');
      div.className = 'top10-item';
      div.innerHTML = `<span><b>${rank + 1}위</b> ${name}</span><span style="font-size:11px; color:#94a3b8;">비교 ➜</span>`;
      div.onclick = () => {
        document.getElementById('aptInput1').value = name;
        fetchAndRender();
      };
      listContainer.appendChild(div);
    });
  }

  async function fetchAndRender() {
    showLoading(true);
    const months = document.getElementById('periodSelect')?.value || 24;
    const slotResults = [];

    try {
      for (let i = 0; i < currentSlotCount; i++) {
        const input = document.getElementById(`aptInput${i+1}`);
        if (!input || isAptEmpty(input.value)) continue;
        const aptName = input.value.trim();
        const cfg = slotConfigs[i];

        try {
          const res = await fetch(`/api/chart-data?apt_name=${encodeURIComponent(aptName)}&months=${months}&area_type=${currentAreaMode}`);
          if (!res.ok) continue;
          const data = await res.json();
          if (data && data.result === 'ok') {
            slotResults.push({ cfg, aptName: data.pure_name || aptName, data });
          }
        } catch(err) {
          console.error("단지 조회 에러:", err);
        }
      }

      globalSlotResults = slotResults;
      if (typeof renderCompareTable === 'function') renderCompareTable(slotResults);
      if (typeof renderTop10Rankings === 'function') renderTop10Rankings(slotResults);

      let allDatesSet = new Set();
      slotResults.forEach(({ data }) => {
        if (data && Array.isArray(data.dates)) {
          data.dates.forEach(d => allDatesSet.add(d));
        }
      });

      const sortedDates = Array.from(allDatesSet).sort();
      const datasets = [];

      slotResults.forEach(({ cfg, aptName, data }) => {
        if (!data || data.result !== 'ok' || !Array.isArray(data.dates)) return;
        const priceMap = {}, maMap = {}, upperMap = {}, lowerMap = {}, detailMap = {};
        data.dates.forEach((d, idx) => {
          priceMap[d] = data.prices[idx]; maMap[d] = data.ma[idx];
          upperMap[d] = data.upper[idx]; lowerMap[d] = data.lower[idx]; detailMap[d] = data.details ? data.details[idx] : null;
        });

        const upperArr = [], lowerArr = [], maArr = [], scatterArr = [], detailsArr = [];
        sortedDates.forEach(d => {
          if (data.built_date && d < data.built_date) {
            upperArr.push(null); lowerArr.push(null); maArr.push(null); scatterArr.push(null); detailsArr.push(null);
          } else {
            upperArr.push(upperMap[d] ?? null); lowerArr.push(lowerMap[d] ?? null);
            maArr.push(maMap[d] ?? null); scatterArr.push(priceMap[d] ?? null); detailsArr.push(detailMap[d] || null);
          }
        });

        datasets.push({ label: `${aptName} BB상단`, typeCategory: 'bb', data: upperArr, borderColor: cfg.color, borderDash: [4,4], borderWidth: 1.2, pointRadius: 0, fill: '+1', backgroundColor: cfg.fill, spanGaps: true, hidden: !visibilityFlags.bb });
        datasets.push({ label: `${aptName} BB하단`, typeCategory: 'bb', data: lowerArr, borderColor: cfg.color, borderDash: [4,4], borderWidth: 1.2, pointRadius: 0, fill: false, spanGaps: true, hidden: !visibilityFlags.bb });
        datasets.push({ label: `${aptName} 이동평균`, typeCategory: 'sma', data: maArr, borderColor: cfg.color, borderWidth: 2.8, pointRadius: 0, fill: false, tension: 0.2, spanGaps: true, hidden: !visibilityFlags.sma });
        datasets.push({ label: `${aptName} 실거래`, typeCategory: 'scatter', type: 'scatter', data: scatterArr, backgroundColor: cfg.color, borderColor: 'transparent', pointRadius: 4.5, pointHoverRadius: 7, hidden: !visibilityFlags.scatter, details: detailsArr, pureName: aptName });
      });

      if (!chart) {
        initVsChart();
      }
      if (chart) {
        chart.data.labels = sortedDates;
        chart.data.datasets = datasets;
        chart.resetZoom();
        chart.update();
      }
    } catch (globalErr) {
      console.error("차트 렌더링 중 오류:", globalErr);
    } finally {
      showLoading(false);
    }
  }

  function toggleCategory(cat) {
    visibilityFlags[cat] = !visibilityFlags[cat];
    const btnMap = { scatter: 'toggleScatterBtn', sma: 'toggleSMABtn', bb: 'toggleBBBtn' };
    document.getElementById(btnMap[cat])?.classList.toggle('active', visibilityFlags[cat]);
    if (chart) {
      chart.data.datasets.forEach(ds => { if (ds.typeCategory === cat) ds.hidden = !visibilityFlags[cat]; });
      chart.update();
    }
  }

  function initVsChart() {
    buildPeriodOptions();
    renderSearchInputs();
    chart = new Chart(document.getElementById('aptChart').getContext('2d'), {
      type: 'line',
      data: { labels: [], datasets: [] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => `📅 계약일자: ${items[0].label}`,
              label: (ctx) => {
                const ds = ctx.dataset;
                const unit = (currentAreaMode === 'all') ? `${ctx.parsed.y}만원/평` : `${ctx.parsed.y}억`;
                if (ds.typeCategory === 'scatter') {
                  const d = ds.details ? ds.details[ctx.dataIndex] : null;
                  if (d) {
                    return `🏢 ${ds.pureName}: ${unit} (전용 ${d.excluUseAr}㎡ / ${d.floor}층)`;
                  }
                  return `🏢 ${ds.pureName}: ${unit}`;
                }
                return `${ds.label}: ${unit}`;
              }
            }
          },
          zoom: {
            pan: { enabled: true, mode: 'y', modifierKey: null },
            zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'y' }
          }
        },
        scales: {
          x: { grid: { color: '#f1f5f9' }, ticks: { maxTicksLimit: 14 } },
          y: { title: { display: true, text: '실거래 가격 (억 원)' }, grid: { color: '#f1f5f9' } }
        }
      }
    });
  }

  window.onload = () => {
    buildYearButtons();
    navigateTo('home');
  };
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
  return UI_HTML