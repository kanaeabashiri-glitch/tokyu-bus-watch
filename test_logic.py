"""
check_status() の判定ロジックを、実際に tokyu.bus-location.jp から取得した
ページ構造（停留所の並び・バスマーカーの出方）を模したフィクスチャで検証する。
ネットワークアクセスは行わない（オフラインの単体テスト）。
"""

from bus_notify import check_status, STOPS_IN_ORDER

STOP_ROW = '<td><a href="javascript:void(0);">{name}</a></td>'
BUS_MARKER = (
    '<tr><td colspan="9">'
    '<img src="https://tokyu.bus-location.jp/blsys/buslocation/images/PC_00.png" alt="バス">'
    ' {label}</td></tr>'
)
LEGEND = """
<h3>【凡例】<a href="#">その他の凡例はこちら</a> 20:10時点の情報</h3>
<table>
<tr><td><img alt="停留所名称アイコン"></td><td>停留所名称（クリックで所要時間表示）</td>
    <td><img alt="停留所アイコン"></td><td>停留所 / 選択停留所（クリックで乗り場地図表示）</td></tr>
<tr><td><img alt="進行方向アイコン"></td><td>進行方向</td>
    <td><img alt="通過停留所アイコン"></td><td>通過停留所</td></tr>
<tr><td><img alt="バスアイコン"></td><td>バス</td>
    <td><img alt="折り返しアイコン"></td><td>終点到着後の折り返しの行き先表示</td></tr>
</table>
"""


def build_page(bus_after_index_list):
    """
    bus_after_index_list: バスマーカーを挿入する位置。
      値 i は「STOPS_IN_ORDER[i] と STOPS_IN_ORDER[i+1] の間」を意味する。
    """
    parts = ["<html><body><table>"]
    for i, stop in enumerate(STOPS_IN_ORDER):
        parts.append(STOP_ROW.format(name=stop))
        if i in bus_after_index_list:
            parts.append(BUS_MARKER.format(label="(折)多摩川行" if i == 0 else "多摩川行"))
    parts.append("</table>")
    parts.append(LEGEND)
    parts.append("</body></html>")
    return "\n".join(parts)


def run_case(name, bus_after_index_list, expected):
    html = build_page(bus_after_index_list)
    passed, detail = check_status(html, debug=False)
    status = "OK" if passed == expected else "NG"
    print(f"[{status}] {name}: passed={passed} (expected={expected})  {detail}")
    assert passed == expected, f"FAILED: {name}"


if __name__ == "__main__":
    target_idx = STOPS_IN_ORDER.index("玉堤小学校")  # = 4

    # 実際に観測したスナップショット相当:
    #   バス1: 明神池前(1) と 野毛桜堤(2) の間 → 対象(玉堤小学校=4)より手前
    #   バス2: 玉堤小学校(4) と 東京都市大南入口(5) の間 → 対象を通過済み
    run_case("実観測相当（対象停留所を通過済みのバスあり）", [1, target_idx], expected=True)

    # 対象より手前にしかバスがいない場合
    run_case("対象より手前にしかバスがいない", [0, 1], expected=False)

    # バスが1台も走っていない（当日の始発前など）
    run_case("運行バスなし", [], expected=False)

    # 対象停留所ちょうど直後（対象と次の停留所の間）にバスがいる = 通過直後
    run_case("対象停留所の直後にバスがいる", [target_idx], expected=True)

    # 終点付近にバスがいる（最後まで進んでいる）
    run_case("終点付近にバスがいる", [len(STOPS_IN_ORDER) - 2], expected=True)

    print("\nすべてのテストケースが期待通りの結果になりました。")
