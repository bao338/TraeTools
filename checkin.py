#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trae 每日签到脚本（GitHub Actions 版）

原理：
  Trae 网页端的 JWT 只有 8 小时有效期，但真正的会话凭证是 HttpOnly Cookie
  `X-Cloudide-Session`（约 14 天有效）。本脚本通过该 Cookie 调用
  `GetUserToken` 接口换取全新 JWT，再用新 JWT 执行每日签到。

依赖：仅标准库，无第三方依赖。

说明：
  日志会打印签到前后的账号积分余额，用真实余额变化判断是否到账；
  若当天已被其他设备/本机领取，claim 仍返回 code=0，此时余额变化为 0，属正常。

环境变量：
  TRAE_SESSION        账号 1 的 X-Cloudide-Session Cookie（必填；后端兼容单账号部署）
  TRAE_DEVICE_ID      账号 1 的 x-device-id，16 位数字（选填，缺省随机）
  TRAE_SESSION_N      第 N(N≥2) 个账号的会话 Cookie；缺失即停止读取更多账号
  TRAE_DEVICE_ID_N    第 N 个账号的 x-device-id（选填，缺省随机）
  全部账号共享：       FEISHU_WEBHOOK（选填，签到后推送一条汇总）

用法：
  python checkin.py
"""

import datetime
import json
import os
import random
import sys
import time
import urllib.request

BASE = "https://api.trae.cn"


def _post(path, headers, body=""):
    """POST 请求。非 2xx 不抛 HTTPError，而是以 (status, body) 正常返回，便于上层判断原因。"""
    import urllib.error
    req = urllib.request.Request(BASE + path, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def get_token(session: str) -> str:
    """用 X-Cloudide-Session Cookie 换取全新 JWT。"""
    headers = {
        "Cookie": "X-Cloudide-Session=" + session,
        "Referer": "https://www.trae.cn/",
        "Origin": "https://www.trae.cn",
        "User-Agent": "TraeCheckin/1.0",
        "Accept": "application/json, text/plain, */*",
    }
    status, text = _post("/cloudide/api/v3/common/GetUserToken", headers)
    data = json.loads(text)
    token = (data.get("Result") or {}).get("Token")
    if status == 401:
        raise RuntimeError(
            "账号会话已失效(HTTP 401)：X-Cloudide-Session 可能已过期(~14天有效期)。"
            "请在浏览器重新登录 trae.cn 并复制新的 Cookie 值，更新到 TRAE_SESSION 后重试。"
            "原始返回: " + text[:200])
    if status != 200 or not token:
        raise RuntimeError("GetUserToken 失败: HTTP %s %s" % (status, text[:200]))
    return token


def checkin(token: str, device_id: str) -> dict:
    """执行每日签到（claim）。"""
    headers = {
        "Authorization": "Cloud-IDE-JWT " + token,
        "X-User-Region": "cn",
        "x-device-id": device_id,
        "Content-Type": "application/json",
        "User-Agent": "TraeCheckin/1.0",
    }
    status, text = _post("/trae/api/v2/ug/checkin_credits/claim", headers, "{}")
    try:
        return {"http": status, "body": json.loads(text)}
    except json.JSONDecodeError:
        return {"http": status, "body": {"raw": text}}


def _auth_headers(token, device_id):
    return {
        "Authorization": "Cloud-IDE-JWT " + token,
        "X-User-Region": "cn",
        "x-device-id": device_id,
        "Content-Type": "application/json",
        "User-Agent": "TraeCheckin/1.0",
    }


def query_status(token: str, device_id: str) -> dict:
    """查询今日签到状态（checked_in 表示今天是否已领取）。失败返回空 dict。"""
    try:
        status, text = _post("/trae/api/v2/ug/checkin_credits/status", _auth_headers(token, device_id), '{"Request":{}}')
        if status != 200:
            return {}
        return json.loads(text)
    except Exception:
        return {}


def get_balance(token: str, device_id: str):
    """查询账号当前总积分；失败返回 None。用于计算本次签到真实到账积分。"""
    try:
        status, text = _post("/trae/api/v2/pay/ide_user_ent_usage", _auth_headers(token, device_id), '{"Request":{}}')
        if status != 200:
            return None
        data = json.loads(text)
        return data.get("usage_summary", {}).get("total_amount")
    except Exception:
        return None


def _http_post(url, data=None, headers=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def _json_post(url, obj, timeout=15):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return _http_post(url, body, {"Content-Type": "application/json"}, timeout)


def notify_all(title, text):
    """把签到结果推送到已配置的渠道，返回成功的渠道名列表。

    在仓库 Secrets 里配置下列任意一个（可同时配多个，全部都会收到）：
      FEISHU_WEBHOOK    飞书群自定义机器人 webhook
      WECOM_WEBHOOK     企业微信群机器人 webhook
      DINGTALK_WEBHOOK  钉钉群自定义机器人 webhook
      PUSHPLUS_TOKEN    pushplus.plus 的 token（微信收消息）
      SERVERCHAN_KEY    Server酱 SendKey（微信收消息）
      NTFY_TOPIC        ntfy.sh 主题名（手机装 ntfy App 订阅，免注册）
    """
    sent = []

    def run(label, fn):
        try:
            fn()
            sent.append(label)
        except Exception as e:
            print("[推送] %s 失败: %s" % (label, e))

    wh = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if wh:
        run("飞书", lambda: _json_post(wh, {"msg_type": "text", "content": {"text": title + "\n" + text}}))

    wh = os.environ.get("WECOM_WEBHOOK", "").strip()
    if wh:
        run("企业微信", lambda: _json_post(wh, {"msgtype": "text", "text": {"content": title + "\n" + text}}))

    wh = os.environ.get("DINGTALK_WEBHOOK", "").strip()
    if wh:
        run("钉钉", lambda: _json_post(wh, {"msgtype": "text", "text": {"content": title + "\n" + text}}))

    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if token:
        run("PushPlus", lambda: _json_post("https://www.pushplus.plus/send", {
            "token": token, "title": title,
            "content": text.replace("\n", "<br>"), "template": "html"}))

    key = os.environ.get("SERVERCHAN_KEY", "").strip()
    if key:
        def _sc():
            import urllib.parse
            body = urllib.parse.urlencode({"title": title, "desp": text}).encode("utf-8")
            _http_post("https://sctapi.ftqq.com/%s.send" % key, body,
                       {"Content-Type": "application/x-www-form-urlencoded"})
        run("Server酱", _sc)

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        run("ntfy", lambda: _http_post("https://ntfy.sh/" + topic, text.encode("utf-8"),
                                       {"Title": "Trae Checkin", "Tags": "white_check_mark"}))

    return sent


def beijing_now_str():
    """返回北京时间字符串（GitHub Actions 运行在 UTC，需 +8 小时）。"""
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def iter_sessions():
    """按顺序产出 (账号序号, session, device_id)。账号 1 读 TRAE_SESSION；
    之后依次读 TRAE_SESSION_2, TRAE_SESSION_3… 直到缺空为止。"""
    s = os.environ.get("TRAE_SESSION", "").strip()
    if s:
        yield 1, s, os.environ.get("TRAE_DEVICE_ID", "").strip()
    n = 2
    while True:
        s = os.environ.get("TRAE_SESSION_%d" % n, "").strip()
        if not s:
            break
        yield n, s, os.environ.get("TRAE_DEVICE_ID_%d" % n, "").strip()
        n += 1


def random_device_id():
    """随机生成 16 位数字风控设备号（仅在缺省时兜底）。"""
    return str(random.randint(10**15, 10**16 - 1))


def main():
    accounts = list(iter_sessions())
    if not accounts:
        print("错误：缺少环境变量 TRAE_SESSION")
        sys.exit(1)

    ok_names, fail_names, detail = [], [], []
    all_ok = True

    for index, session, device_id in accounts:
        # 多账号之间错开 3~6 秒随机间隔，降低请求密度，规避 9074「参与用户太多」风控
        if index > 1:
            time.sleep(random.uniform(3, 6))
        name = "账号 %d" % index
        device_id = device_id or random_device_id()
        print("[%s] device_id=%s" % (name, device_id))
        try:
            token = get_token(session)
            print("[%s] 已换取新 JWT，长度=%d" % (name, len(token)))
            before = query_status(token, device_id)
            bal_before = get_balance(token, device_id)
            print("[%s] 签到前：今日已签到=%s 积分余额=%s" % (
                name, before.get("checked_in"), "-" if bal_before is None else bal_before))
            result = checkin(token, device_id)
            body = result["body"]
            code = body.get("code", -1)
            # 9074「参与用户太多」= 设备号被风控标记；换全新设备号自动重试（最多 5 次）
            attempt = 1
            while code == 9074 and attempt < 5:
                device_id = random_device_id()
                attempt += 1
                print("[%s] 命中风控 9074，换新设备号重试（第 %d 次）" % (name, attempt))
                time.sleep(random.uniform(0.8, 1.5))
                result = checkin(token, device_id)
                body = result["body"]
                code = body.get("code", -1)
            after = query_status(token, device_id)
            bal_after = get_balance(token, device_id)
            checked = after.get("checked_in")
            if checked is None:
                checked = body.get("checked_in", False)
            ok = (result["http"] == 200) and (code == 0 or checked) and (checked is not False)
            if bal_before is not None and bal_after is not None:
                delta = bal_after - bal_before
            else:
                delta = None
            if ok:
                if delta is None:
                    gain = "积分变化未知"
                elif delta > 0:
                    gain = "本次 +%s 积分" % delta
                else:
                    gain = "本次 +0（今天已由其他端领取过）"
                line = "[%s] ✓ 余额 %s → %s，%s" % (
                    name, "-" if bal_before is None else bal_before,
                    "-" if bal_after is None else bal_after, gain)
                print(line)
                detail.append(line)
                ok_names.append(name)
            else:
                reason = body.get("message") or ("HTTP %s" % result["http"])
                line = "[%s] ✗ 失败：%s" % (name, reason)
                print(line)
                detail.append(line)
                fail_names.append(name)
                all_ok = False
        except Exception as e:
            print("[%s] 签到异常: %s" % (name, e))
            detail.append("[%s] ✗ 异常：%s" % (name, e))
            fail_names.append(name)
            all_ok = False

    # 汇总并推送（配置了任一渠道才会真的发出去）
    title = "Trae 签到 %s" % beijing_now_str()
    lines = ["结果：%s" % ("全部成功" if all_ok else "有失败")]
    lines += detail
    sent = notify_all(title, "\n".join(lines))
    if sent:
        print("已推送：%s" % "、".join(sent))
    else:
        print("未推送：未配置推送渠道（或推送失败），见 Secrets 里的 FEISHU_WEBHOOK / WECOM_WEBHOOK 等")

    if not all_ok:
        sys.exit(1)
    print("全部账号签到完成")


if __name__ == "__main__":
    main()
