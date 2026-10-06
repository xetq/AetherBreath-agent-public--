# agent_tools/time_weather.py
"""
联合查询任意地点的当地时间与实时天气。支持城市/国家/地标名称（中英文均可，如 '北京'、'Shanghai'）、经纬度坐标（如 '48.85,2.35'）与 IANA 时区名（如 'Asia/Tokyo'）。通过 mode 参数控制：'time' 仅查当地时间、'weather' 仅查当地天气、'both' 同时查两者。
自动生成于 AetherBreath create_tool。
"""


def _slog(logger, msg: str = "", level: str = "info") -> None:
    """安全日志：日志绝不能把工具本身搞崩（审计 B6）。

    旧代码是 `logger.info(f"xxx 开始", location={ location })` —— 那个 `{ location }`
    是**独立的 set 字面量参数**（不是 f-string 插值）。实测：传标准 logging.Logger
    直接抛 `TypeError: Logger._log() got an unexpected keyword argument`，而该调用
    在 try 之外、异常直穿函数；传项目注入的 SessionLogger 虽不崩，但 set 过不了
    json.dumps，日志被静默吞掉。
    """
    try:
        if logger:
            getattr(logger, level, logger.info)(msg)
    except Exception:
        pass


def time_weather(
    location, mode='both', target_hour=None,
    logger=None,
) -> dict:
    """
    联合查询任意地点的当地时间与实时天气。支持城市/国家/地标名称（中英文均可，如 '北京'、'Shanghai'）、经纬度坐标（如 '48.85,2.35'）与 IANA 时区名（如 'Asia/Tokyo'）。通过 mode 参数控制：'time' 仅查当地时间、'weather' 仅查当地天气、'both' 同时查两者。

    Args:
        location: string - 要查询的地点：城市/国家/地标名（如 '北京'、'Shanghai'、'Eiffel Tower'）、坐标（'48.85,2.35'）或 IANA 时区名（'Asia/Tokyo'，适合仅查时间）
        mode: string - 查询模式：time=仅当地时间；weather=仅当地天气；both=同时查询时间与天气
        logger: 日志实例（由编排器自动注入）

    Returns:
        dict: {"success": bool, "result": Any, "error": str|None}
    """
    _slog(logger, "time_weather 开始: location=%s, mode=%s" % (location, mode))

    try:
        # ===== 用户实现代码 =====
        import json as _json, urllib.request as _ur, urllib.parse as _up
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZoneInfo
        
        WMO = {0:"晴",1:"基本晴朗",2:"局部多云",3:"阴",45:"雾",48:"雾凇",51:"小毛毛雨",53:"毛毛雨",55:"大毛毛雨",56:"冻毛毛雨",57:"强冻毛毛雨",61:"小雨",63:"中雨",65:"大雨",66:"小冻雨",67:"强冻雨",71:"小雪",73:"中雪",75:"大雪",77:"雪粒",80:"小阵雨",81:"中阵雨",82:"强阵雨",85:"小阵雪",86:"强阵雪",95:"雷暴",96:"雷暴伴小冰雹",99:"雷暴伴大冰雹"}
        WEEK = ["周一","周二","周三","周四","周五","周六","周日"]
        
        def _http_json(url, timeout=15):
            req = _ur.Request(url, headers={"User-Agent":"AetherBreath/1.0 (time_weather)"})
            with _ur.urlopen(req, timeout=timeout) as r:
                return _json.loads(r.read().decode("utf-8"))
        
        s = (location or "").strip()
        lat = lon = None
        place_name = None
        country = None
        tz_iana = None
        geo_err = None
        weather = None
        w_err = None
        local_dt = None
        off = None
        z = None
        now = None
        
        # 1) 尝试把输入当坐标
        if lat is None and "," in s:
            try:
                parts = [p.strip() for p in s.split(",")]
                if len(parts) == 2:
                    la = float(parts[0]); lo = float(parts[1])
                    if -90 <= la <= 90 and -180 <= lo <= 180:
                        lat, lon = la, lo
                        place_name = "%.4f,%.4f" % (la, lo)
            except ValueError:
                pass
        
        # 2) 地理编码：地名 -> 坐标 + 时区
        if lat is None:
            try:
                lang = "zh" if any("一" <= ch <= "鿿" for ch in s) else "en"
                g = _http_json("https://geocoding-api.open-meteo.com/v1/search?count=10&language=" + lang + "&format=json&name=" + _up.quote(s))
                res_list = g.get("results") or []
                if res_list:
                    res = sorted(res_list, key=lambda x: x.get("population") or 0, reverse=True)[0]
                    lat = res.get("latitude"); lon = res.get("longitude")
                    place_name = res.get("name") or s
                    country = res.get("country")
                    tz_iana = res.get("timezone")
                else:
                    geo_err = "地理编码未找到该地点，请尝试更具体的地点名或直接提供 IANA 时区名"
            except Exception as e:
                geo_err = "地理编码请求失败: %s: %s" % (type(e).__name__, e)
        
        # 3) 时区兜底：**能拿到 IANA 时区名就直接算当地时间**，不必拉天气接口。
        #    审计 B20：旧实现把这段逻辑关在 `lat is None` 里面，于是
        #    `mode="time"` + 城市名（有坐标）时，本地时间只能从天气接口的
        #    `current.time` 里取 —— 为问个时间而发一次天气请求。
        z = None
        if mode in ("time", "both"):
            tz_try = tz_iana or (s if lat is None else None)
            if tz_try:
                try:
                    z = _ZoneInfo(tz_try)
                    now = _dt.now(z)
                except Exception:
                    z = None
            if z is None and lat is None:
                # Windows 缺 IANA 时区库时，退回 worldtimeapi 拿带偏移的当前时间。
                # 审计 B20：改 https（旧实现走明文 http，时间可被中间人篡改）。
                try:
                    wt = _http_json("https://worldtimeapi.org/api/timezone/"
                                    + _up.quote(s, safe="/"), timeout=10)
                    now = _dt.fromisoformat(wt["datetime"].replace("Z", "+00:00"))
                    z = "fallback"
                except Exception:
                    z = None
        
        result = {"location_input": location, "mode": mode, "parsed_place": place_name, "country": country}
        
        # 4) 有坐标 -> 拉取 forecast（一次拿到当地时间+时区+天气）
        # 审计 B20：`mode="time"` 只问时间，不该顺带发一次天气请求。
        if lat is not None and mode in ("weather", "both"):
            wu = ("https://api.open-meteo.com/v1/forecast?latitude=%.5f&longitude=%.5f"
              "&current=temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,wind_speed_10m,wind_direction_10m"
              "&hourly=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation_probability,weather_code,wind_speed_10m"
              "&timezone=auto&forecast_days=3") % (lat, lon)
            try:
                w = _http_json(wu, timeout=20)
                hourly_data = w.get("hourly") or {}
                h_times = hourly_data.get("time") or []
                tz_iana = tz_iana or w.get("timezone")
                off = w.get("utc_offset_seconds")
                cur_for_time = w.get("current") or {}
                if cur_for_time.get("time"):
                    local_dt = _dt.fromisoformat(cur_for_time["time"])
                if target_hour:
                    target_iso = target_hour.strip().replace(" ", "T")
                    if len(target_iso) >= 16 and target_iso[13] == ":":
                        target_iso = target_iso[:16]
                    elif len(target_iso) == 13:
                        target_iso = target_iso + ":00"
                    if target_iso in h_times:
                        hi = h_times.index(target_iso)
                        def _hget(key):
                            arr = hourly_data.get(key) or []
                            return arr[hi] if hi < len(arr) else None
                        wmo = _hget("weather_code")
                        is_day = 1 if 6 <= int(target_iso[11:13]) < 18 else 0
                        weather = {
                            "description": WMO.get(wmo, "未知天气代码%d" % wmo if wmo is not None else None),
                            "temperature_c": _hget("temperature_2m"),
                            "feels_like_c": _hget("apparent_temperature"),
                            "humidity_percent": _hget("relative_humidity_2m"),
                            "wind_speed_kmh": _hget("wind_speed_10m"),
                            "precipitation_probability_percent": _hget("precipitation_probability"),
                            "day_night": "白天" if is_day == 1 else "夜间",
                            "target_hour": target_iso,
                        }
                    else:
                        weather = {"error": "未找到目标时刻 %s 的预报数据" % target_iso}
                else:
                    cur = w.get("current") or {}
                    tz_iana = tz_iana or w.get("timezone")
                    off = w.get("utc_offset_seconds")
                    if cur.get("time"):
                        local_dt = _dt.fromisoformat(cur["time"])
                    wmo = cur.get("weather_code")
                    is_day = cur.get("is_day")
                    weather = {
                        "description": WMO.get(wmo, "未知天气代码%d" % wmo if wmo is not None else None),
                        "temperature_c": cur.get("temperature_2m"),
                        "feels_like_c": cur.get("apparent_temperature"),
                        "humidity_percent": cur.get("relative_humidity_2m"),
                        "wind_speed_kmh": cur.get("wind_speed_10m"),
                        "wind_direction_deg": cur.get("wind_direction_10m"),
                        "precipitation_mm": cur.get("precipitation"),
                        "day_night": "白天" if is_day == 1 else ("夜间" if is_day == 0 else None),
                    }
            except Exception as e:
                weather = None
                w_err = "天气接口请求失败: %s: %s" % (type(e).__name__, e)
        else:
            w_err = geo_err or "缺少坐标信息，无法查询天气"
        
        # 5) 时间信息组装
        timeinfo = None
        if mode in ("time", "both"):
            if lat is not None and local_dt is not None:
                timeinfo = {
                    "local": {"date": local_dt.strftime("%Y-%m-%d"), "weekday": WEEK[local_dt.weekday()], "time": local_dt.strftime("%H:%M:%S"), "iso": local_dt.isoformat()},
                    "timezone": tz_iana,
                    "utc_offset_seconds": off,
                }
            elif z is not None and now is not None:
                timeinfo = {
                    "local": {"date": now.strftime("%Y-%m-%d"), "weekday": WEEK[now.weekday()], "time": now.strftime("%H:%M:%S"), "iso": now.isoformat(timespec="seconds")},
                    "timezone": tz_iana or s,
                    "utc_offset_seconds": int(now.utcoffset().total_seconds()),
                }
            else:
                timeinfo = {"error": "无法确定该地点的时区（请尝试更具体的地点名，或直接提供 IANA 时区如 Asia/Shanghai）"}
        
        # 6) 按模式裁剪
        if mode == "time":
            result["time"] = timeinfo
        elif mode == "weather":
            result["weather"] = weather if weather is not None else {"error": w_err}
        else:
            result["time"] = timeinfo if timeinfo is not None else {"error": "时间查询不可用"}
            result["weather"] = weather if weather is not None else {"error": w_err}
        
        # 7) 人类可读摘要
        parts2 = []
        tobj = result.get("time")
        if isinstance(tobj, dict) and not tobj.get("error"):
            l2 = tobj["local"]
            parts2.append("%s 当地时间：%s %s %s（时区 %s）" % (place_name or s, l2["date"], l2["weekday"], l2["time"], tobj["timezone"]))
        wobj = result.get("weather")
        if isinstance(wobj, dict) and not wobj.get("error"):
            label = "%s %s 天气" % (place_name or s, wobj.get("target_hour", "实时"))
            precip = "降水概率 %s%%" % wobj["precipitation_probability_percent"] if wobj.get("precipitation_probability_percent") is not None else "降水量 %smm" % wobj.get("precipitation_mm", "?")
            parts2.append("%s：%s，%s°C（体感 %s°C），湿度 %s%%，风速 %s km/h，%s，%s" % (
                label, wobj["description"], wobj["temperature_c"], wobj["feels_like_c"],
                wobj["humidity_percent"], wobj["wind_speed_kmh"], wobj.get("day_night") or "", precip,
            ))
        result["summary"] = "；".join([p for p in parts2 if p]) if parts2 else "查询失败，详见错误字段"
        result = result
        # ===== 结束 =====
        _slog(logger, "time_weather 完成: " + str(result)[:200])
        return {"success": True, "result": result, "error": None}
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        _slog(logger, "time_weather 失败: " + error_msg, level="error")
        return {"success": False, "result": None, "error": error_msg}


time_weather_schema = {
    "type": "function",
    "function": {
        "name": "time_weather",
        "description": "联合查询任意地点的当地时间与实时天气。支持城市/国家/地标名称（中英文均可，如 '北京'、'Shanghai'）、经纬度坐标（如 '48.85,2.35'）与 IANA 时区名（如 'Asia/Tokyo'）。通过 mode 参数控制：'time' 仅查当地时间、'weather' 仅查当地天气、'both' 同时查两者。",
        "parameters": {
            "type": "object",
            "properties": {
    "location": {
        "type": "string",
        "description": "要查询的地点：城市/国家/地标名（如 '北京'、'Shanghai'、'Eiffel Tower'）、坐标（'48.85,2.35'）或 IANA 时区名（'Asia/Tokyo'，适合仅查时间）"
    },
    "mode": {
        "type": "string",
        "enum": [
            "time",
            "weather",
            "both"
        ],
        "default": "both",
        "description": "查询模式：time=仅当地时间；weather=仅当地天气；both=同时查询时间与天气"
    },
    "target_hour": {
        "type": "string",
        "default": None,
        "description": "可选，指定查询未来某时刻的天气（格式 YYYY-MM-DD HH:MM 或 YYYY-MM-DDTHH:MM，如 2026-09-04 14:00）。不指定则查询当前实时天气"
    }
},
            "required": ["location"]
        }
    }
}

__all__ = ["time_weather", "time_weather_schema"]
