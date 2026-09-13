#!/usr/bin/env python3
"""
bus_notify.py

東急バス「玉11」系統（二子玉川駅 → 多摩川駅）が「玉堤小学校」停留所を
通過したことを検知したら、LINE Messaging API (broadcast) で通知するスクリプト。

■ 想定している運用
  - GitHub Actions などのスケジューラで毎朝 6:00 JST 前後に起動する
  - 起動後、6:40 JST まで POLL_INTERVAL_SEC 間隔で東急バスナビをチェックし続ける
  - 「玉堤小学校」をバスが通過したと判定した時点で LINE に1回だけ通知して終了する
  - 6:40 JST を過ぎても検知できなければ、何も送らず終了する
    （ただし一度もページ取得・判定に成功しなかった場合は、異常を知らせる通知を送る）

■ 使い方
  通常運用（監視ループ）:
      python bus_notify.py

  動作確認用（1回だけ判定して結果を表示。LINE未設定でもOK、時間帯制限もなし）:
      python bus_notify.py --once
      python bus_notify.py --once --debug   # 判定根拠のHTML抜粋も表示

■ 環境変数
  LINE_CHANNEL_ACCESS_TOKEN   LINE Messaging API のチャネルアクセストークン（長期）
                              通常運用時は必須。--once では未設定でも動作確認可。
  POLL_INTERVAL_SEC           ポーリング間隔（秒）。デフォルト 30

■ 判定の仕組み（重要・要理解）
  東急バスナビの路線別運行情報ページ（PC版）は、JavaScriptなしでも最新のバス位置が
  サーバー側で描画された状態のHTMLが返ってくることを確認済みです（取得のたびに
  ページ内の「時点の情報」の時刻が更新される）。そのため Playwright 等のブラウザ
  自動化は使わず、素の HTTP GET だけで動作します。

  ページ内では、停留所名が上から順（二子玉川駅→…→多摩川駅）に並んでおり、
  現在運行中のバスがいる区間には「バスアイコン＋『多摩川行』などの方面表示」が
  停留所と停留所の間の行として挿入されます。これは実際に運行中のバス表示で
  確認済みのパターンです。

  したがって「対象停留所（玉堤小学校）よりページ内で後ろの位置に、同方向
  （多摩川行）のバスマーカーが存在するか」を見れば、通過済みかどうかを
  判定できます。HTMLの生テキスト中の出現位置（文字インデックス）の前後関係を
  比較しているだけの、シンプルなヒューリスティックです。

  ※ これは公式APIではなく、公開ページの表示パターンを解析した非公式な方法です。
    サイトのリニューアル等で表示が変わると判定が壊れる可能性があります。
    その場合は check_status() 内のロジックを、実際に取得したHTMLを見ながら
    調整してください（README参照）。
"""

import argparse
import datetime
import os
import re
import sys
import time
import zoneinfo

import requests

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

# 東急バスナビ「玉11」系統（二子玉川駅～多摩川駅）路線別運行情報ページ（PC版）
# RAMK=55 が系統「玉11」を表す。VID=rtl / SCT=1 で路線全体の停留所一覧+バス位置を表示。
BUS_PAGE_URL = "https://tokyu.bus-location.jp/blsys/navi?EID=nt&RAMK=55&SCT=1&VID=rtl"

# 二子玉川駅 → 多摩川駅 の停留所順（ページの表示順と一致させてあります）
STOPS_IN_ORDER = [
    "二子玉川駅",
    "明神池前",
    "野毛桜堤",
    "野毛二丁目",
    "玉堤小学校",
    "東京都市大南入口",
    "玉堤一丁目",
    "玉川温室村",
    "田園調布五丁目",
    "多摩川グランド前",
    "パークウェイ入口",
    "多摩川台公園",
    "多摩川駅",
]

TARGET_STOP = "玉堤小学校"          # 通知したい停留所
DIRECTION_LABEL = "多摩川行"         # 二子玉川→多摩川方面のバスマーカーに付くラベル

WATCH_START = datetime.time(6, 0)   # 監視（通知を送ってよい）開始時刻 JST
WATCH_END = datetime.time(6, 40)    # 監視終了時刻 JST
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "30"))

JST = zoneinfo.ZoneInfo("Asia/Tokyo")


# ---------------------------------------------------------------------------
# ページ取得・判定
# ---------------------------------------------------------------------------

def fetch_page_html() -> str:
    resp = requests.get(
        BUS_PAGE_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; bus-watch-script/1.0)"},
        timeout=15,
    )
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def check_status(html: str, debug: bool = False) -> tuple[bool, str]:
    """
    HTML中のテキスト出現順を使って、玉堤小学校をバスが通過済みかどうかを判定する。

    戻り値: (passed, detail)
      passed: 通過済みと判定したら True
      detail: ログ用の説明文字列
    """
    # ページ末尾付近の「凡例」セクションは、本文と紛らわしい語（バス等）を
    # 含みうるので判定対象から外す。ただし「凡例」という文字列がページ前半に
    # 出てくる想定外の構造だった場合に誤って本文を消さないよう、後半にしか
    # 出てこないときだけ切り詰める。
    legend_pos = html.find("凡例")
    if legend_pos != -1 and legend_pos > len(html) * 0.5:
        main_html = html[:legend_pos]
    else:
        main_html = html

    missing = [s for s in STOPS_IN_ORDER if s not in main_html]
    if missing:
        raise RuntimeError(
            "ページ内に想定した停留所名が見つかりません: " + ", ".join(missing) +
            " ／ サイトの構造が変わった可能性があります。"
        )

    target_pos = main_html.find(TARGET_STOP)

    hit_positions = []
    for m in re.finditer(re.escape(DIRECTION_LABEL), main_html):
        window = main_html[max(0, m.start() - 300): m.start()]
        if "バス" in window or "PC_00" in window:
            hit_positions.append(m.start())

    passed = any(p > target_pos for p in hit_positions)

    detail = (
        f"target_pos={target_pos}, "
        f"bus_marker_positions={hit_positions}"
    )
    if debug:
        snippet = main_html[max(0, target_pos - 80): target_pos + 200]
        detail += "\n--- 対象停留所付近のHTML抜粋 ---\n" + snippet

    return passed, detail


# ---------------------------------------------------------------------------
# LINE通知
# ---------------------------------------------------------------------------

def send_line_broadcast(text: str) -> None:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("環境変数 LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")
    resp = requests.post(
        "https://api.line.me/v2/bot/message/broadcast",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        json={"messages": [{"type": "text", "text": text}]},
        timeout=15,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# 実行モード
# ---------------------------------------------------------------------------

def run_once(debug: bool) -> int:
    """1回だけ判定して結果を表示する（動作確認用。通知は送らない）。"""
    now = datetime.datetime.now(JST)
    try:
        html = fetch_page_html()
        passed, detail = check_status(html, debug=debug)
    except Exception as e:
        print(f"[{now.isoformat()}] エラー: {e}", file=sys.stderr)
        return 1
    print(f"[{now.isoformat()}] {TARGET_STOP} 通過判定: {passed}")
    print(detail)
    return 0


def run_watch_loop() -> int:
    """6:40 JST まで監視を続け、通過を検知したら通知して終了する。"""
    now = datetime.datetime.now(JST)
    print(f"[{now.isoformat()}] 監視開始（{WATCH_END.strftime('%H:%M')} JSTまで）")

    ok_checks = 0
    total_checks = 0

    while True:
        now = datetime.datetime.now(JST)
        current_time = now.timetz().replace(tzinfo=None)

        if current_time > WATCH_END:
            print(
                f"[{now.isoformat()}] 監視終了時刻を過ぎたため終了します。"
                f"（チェック成功 {ok_checks}/{total_checks} 回）"
            )
            if total_checks > 0 and ok_checks == 0:
                # 一度もページ取得・判定に成功しなかった＝おそらく異常。知らせておく。
                try:
                    send_line_broadcast(
                        "⚠️ バス通過チェックが本日一度も成功しませんでした。"
                        "サイト構造が変わっている可能性があります。ログを確認してください。"
                    )
                except Exception as e:
                    print(f"異常通知の送信にも失敗しました: {e}", file=sys.stderr)
            return 0

        total_checks += 1
        try:
            html = fetch_page_html()
            passed, detail = check_status(html)
            ok_checks += 1
        except Exception as e:
            print(f"[{now.isoformat()}] チェック中にエラー: {e}", file=sys.stderr)
            passed, detail = False, ""

        print(f"[{now.isoformat()}] 通過判定: {passed}  {detail}")

        if passed and current_time >= WATCH_START:
            send_line_broadcast(
                f"🚌 バスが「{TARGET_STOP}」を通過しました。"
                f"（{now.strftime('%H:%M')} 時点で検知）"
            )
            print(f"[{now.isoformat()}] LINE通知を送信しました。終了します。")
            return 0
        elif passed:
            print(f"[{now.isoformat()}] 通過を検知しましたが監視開始時刻前のため通知は保留します。")

        time.sleep(POLL_INTERVAL_SEC)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true",
        help="1回だけ判定して終了する（通知なし・時間帯制限なし。動作確認用）",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="判定根拠のHTML抜粋も表示する（--once と併用）",
    )
    args = parser.parse_args()

    if args.once:
        return run_once(debug=args.debug)
    return run_watch_loop()


if __name__ == "__main__":
    sys.exit(main())
