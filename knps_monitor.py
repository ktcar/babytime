#!/usr/bin/env python3
"""국립공원 야영장 잔여석 감시 (조회 + 알림 전용, 자동 예약 없음).

환경변수
  KNPS_ID, KNPS_PW                      국립공원 예약시스템 로그인 정보 (필수)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  텔레그램 알림 (선택)
  OPEN_BROWSER=0                        자리 발견 시 브라우저 자동 열기 끄기 (기본 켜짐)
  RUN_SECONDS                           이 시간(초)만 돌고 종료 (GitHub Actions 작업 시간 제한용)
  NOTIFY_START=1                        시작 시 텔레그램으로 첫 조회 결과 전송
"""
import os
import re
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ---- 감시 대상 ----
PARK_ID = "B22"          # 태백산
DEPT_ID = "B221004"      # 소도
CATEGORY = "카라반"
DATE = "20261010"        # 2026-10-10
INTERVAL = 180           # 3분
JITTER = 30              # 0~30초 랜덤 추가 지연

KST = timezone(timedelta(hours=9))
END_AT = datetime(2026, 10, 10, 18, 0, tzinfo=KST)

BASE = "https://res.knps.or.kr"
REMAIN_PAGE = BASE + "/reservation/searchCampRemainSite.do"
RESERVE_PAGE = BASE + "/reservation/searchSimpleCampReservation.do"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


def log(msg):
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


class SessionExpired(Exception):
    pass


class Client:
    def __init__(self, user, pw):
        self.user, self.pw = user, pw
        self.s = None

    def login(self):
        s = requests.Session()
        s.headers["User-Agent"] = UA
        s.get(BASE + "/mmb/mmbLogin.do", timeout=20)
        s.post(BASE + "/mmb/mmbLoginProc.do", timeout=20,
               data={"loginType": "Member", "mmbId": self.user, "passWd": self.pw},
               headers={"Referer": BASE + "/mmb/mmbLogin.do"})
        r = s.get(REMAIN_PAGE, timeout=20)
        if "로그인후 이용할 수 있습니다" in r.text:
            raise RuntimeError("로그인 실패 (아이디/비밀번호 또는 추가 인증 확인 필요)")
        self.s = s
        log("로그인 성공")

    def fetch(self):
        if self.s is None:
            self.login()
        r = self.s.post(BASE + "/reservation/selectCampRemainSiteList.do", timeout=20,
                        data={"prd_sal_ymd": DATE, "park": PARK_ID},
                        headers={"X-Requested-With": "XMLHttpRequest", "Referer": REMAIN_PAGE})
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            raise SessionExpired("JSON 아님 (세션 만료 추정)")
        # 비로그인/세션 만료 시 서버는 {"list": null} 을 돌려준다
        if not data.get("list"):
            raise SessionExpired("빈 목록 (세션 만료 추정)")
        for row in data["list"]:
            if row.get("deptId") == DEPT_ID and row.get("prdCtgNm") == CATEGORY:
                return int(row["cntN"]), int(row["cntW"]), int(row["cntC"]), int(row["cntR"])
        raise ValueError(f"응답에 {DEPT_ID}/{CATEGORY} 행이 없음")

    def check(self):
        try:
            return self.fetch()
        except SessionExpired as e:
            log(f"{e} → 재로그인")
            self.s = None
            return self.fetch()


def bot_token():
    """붙여넣기 실수(공백, 따옴표, 앞의 bot)를 정리한 텔레그램 봇 토큰."""
    # 휴대폰에서 줄바꿈된 토큰을 복사하면 중간에 공백/줄바꿈이 섞이므로 모두 제거
    t = "".join(os.environ.get("TELEGRAM_BOT_TOKEN", "").split()).strip('"\'')
    return t[3:] if t.lower().startswith("bot") else t


def notify_telegram(text):
    token = bot_token()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=15,
                          data={"chat_id": chat, "text": text})
        if not r.ok:
            desc = r.json().get("description", "") if "json" in r.headers.get("Content-Type", "") else ""
            log(f"텔레그램 전송 실패: HTTP {r.status_code} {desc}")
        return r.ok
    except requests.RequestException as e:
        log(f"텔레그램 전송 오류: {type(e).__name__}")
        return False


def notify_mac(title, body):
    if sys.platform != "darwin":
        return
    script = f'display notification "{body}" with title "{title}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False)
    for _ in range(3):
        subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False)
    if os.environ.get("OPEN_BROWSER", "1") != "0":
        subprocess.run(["open", RESERVE_PAGE], check=False)


def alert(n, w):
    body = f"태백산 소도 {CATEGORY} 2026-10-10 예약가능 {n}석 (대기가능 {w})"
    log("🔔 알림: " + body)
    notify_telegram(f"🏕️ {body}\n예약: {RESERVE_PAGE}")
    notify_mac("야영장 자리 발생!", body)


def main():
    user, pw = os.environ.get("KNPS_ID"), os.environ.get("KNPS_PW")
    if not (user and pw):
        sys.exit("KNPS_ID / KNPS_PW 환경변수를 설정하세요.")
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        log("TELEGRAM_BOT_TOKEN 없음 → 텔레그램 알림 생략")

    client = Client(user, pw)
    alerted = False
    end_at = END_AT
    if os.environ.get("RUN_SECONDS"):
        end_at = min(END_AT, datetime.now(KST) + timedelta(seconds=int(os.environ["RUN_SECONDS"])))
    notify_start = os.environ.get("NOTIFY_START") == "1"
    errors = 0  # 연속 오류 수
    ran = False
    log(f"감시 시작: 태백산 소도 {CATEGORY} {DATE}, 종료 {end_at:%m-%d %H:%M} KST")
    while datetime.now(KST) < end_at:
        ran = True
        try:
            n, w, c, r = client.check()
            if errors >= 5:
                notify_telegram("✅ 조회가 다시 정상입니다.")
            errors = 0
            log(f"예약가능 {n} / 대기가능 {w} / 예약만료 {c} / 예약불가 {r}")
            if notify_start:
                notify_telegram(f"✅ 감시 시작 (태백산 소도 {CATEGORY} 10/10)\n"
                                f"현재 예약가능 {n} / 대기가능 {w} / 예약만료 {c} / 예약불가 {r}\n"
                                f"3분마다 확인, 자리 나면 바로 알려드려요.")
                notify_start = False
            if n >= 1 and not alerted:
                alert(n, w)
                alerted = True
            elif n == 0 and alerted:
                log("다시 0석 → 다음에 생기면 재알림")
                alerted = False
        except Exception as e:  # 어떤 오류든 기록 후 계속
            log(f"조회 오류: {type(e).__name__}: {e}")
            client.s = None
            errors += 1
            if notify_start:
                notify_telegram(f"⚠️ 감시를 시작했지만 첫 조회에 실패했습니다: {type(e).__name__}: {e}\n계속 재시도합니다.")
                notify_start = False
            if errors == 5:
                notify_telegram(f"⚠️ 5번 연속 조회 실패 중: {type(e).__name__}: {e}\n계속 재시도합니다.")
        wait = INTERVAL + random.uniform(0, JITTER)
        remaining = (end_at - datetime.now(KST)).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(wait, remaining))
    log("종료 시각 도달 → 감시 종료")
    if ran and datetime.now(KST) >= END_AT:
        notify_telegram("⏹️ 10/10 18시가 지나 감시를 종료했습니다.")


def check():
    """설정 점검: 비밀값 유무, 봇 이름, 텔레그램 전송, 사이트 1회 조회 (값 자체는 출력하지 않음)."""
    for k in ("KNPS_ID", "KNPS_PW", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        v = os.environ.get(k, "")
        log(f"{k}: {'설정됨' if v else '없음'} (길이 {len(v)})")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    log(f"TELEGRAM_CHAT_ID 숫자 여부: {chat.lstrip('-').isdigit()}")
    raw = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    token = bot_token()
    head, _, tail = token.partition(":")
    log(f"토큰 모양: 원래 길이 {len(raw)}, 정리 후 {len(token)}, ':' 있음 {bool(_)}, "
        f"앞부분 숫자 {head.isdigit()}({len(head)}자), 뒷부분 {len(tail)}자, "
        f"공백/줄바꿈 포함 {any(ch.isspace() for ch in raw)}, 따옴표 포함 {any(ch in raw for ch in chr(34) + chr(39))}, "
        f"정상 형식 {bool(re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{30,}', token))}")
    if token:
        try:
            me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
            log(f"봇 확인: ok={me.get('ok')} 봇=@{me.get('result', {}).get('username')} {me.get('description', '')}")
        except requests.RequestException as e:
            log(f"봇 확인 오류: {type(e).__name__}")
    log(f"텔레그램 테스트 전송: {notify_telegram('🔧 설정 점검: 이 메시지가 보이면 텔레그램 알림이 정상입니다.')}")
    try:
        c = Client(os.environ["KNPS_ID"], os.environ["KNPS_PW"])
        n, w, cc, r = c.check()
        log(f"사이트 조회 성공: 예약가능 {n} / 대기가능 {w} / 예약만료 {cc} / 예약불가 {r}")
    except Exception as e:
        log(f"사이트 조회 실패: {type(e).__name__}: {e}")


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
        sys.exit()
    try:
        main()
    except KeyboardInterrupt:
        log("사용자 중지")
