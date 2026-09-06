# 通話ステップ（DTMF / REFER）と記録表出力のレポート

作成日: 2026-09-06
対象: 設計 段階 5 の「DTMF / REFER ステップ」と、設計 3.6 の「記録表の xlsx 出力」。

## 通話ステップ

シナリオの `caller.steps` を発信側の通話ごとに実行する。`call_duration` は安全上の上限として残る。

| ステップ | 内容 | 例 |
|---|---|---|
| `hold` | 指定秒数維持。相手が切れば終了 | `- hold: 30s` |
| `dtmf` | RFC 2833 の telephone-event で送出（100 ms、間隔 60 ms。`duration_ms` / `gap_ms` で変更）。送出中は音声を止める | `- dtmf: "*4"` |
| `refer` | 相手レグのブラインド転送（RFC 3515）。REFER → 202 → NOTIFY(sipfrag 200) → PBX からの BYE を待つ | `- refer: "9004"` |
| `bye` | 発信側から切断 | `- bye` |

### FreeSWITCH での確認

DTMF: 9196（echo）に発信し、`hold 1s → dtmf "1234#" → hold 1s → bye`。FreeSWITCH のログ（INFO）に
`RECV DTMF 1:800`、`2:800`、`3:800`、`4:800`、`#:800` が 240 ms 間隔で記録された（800 サンプル = 100 ms）。
SDP は `m=audio ... 0 8 101` / `a=rtpmap:101 telephone-event/8000` で交渉。

REFER: 9100 → 8001 → 9001 応答の後、`hold 2s → refer "9004"`。発信側は REFER に 202、NOTIFY（sipfrag 200 OK）を受けて PBX から BYE。
9004 には 9001 からの INVITE が届いて応答し、転送が成立した。

ループバック（PBX なし）の単体テストで、RFC 2833 のパケット列（進行 5 パケット + 終了 3 パケット、マーカー、固定タイムスタンプ、duration 800）と、
ステップ実行（hold / dtmf / bye、相手切断時の中断）を検証している。

## 記録表（xlsx）

`semishigure report --runs 3,4,5 -o report.xlsx`、または画面の Past runs からダウンロード（チェックしたラン、未選択なら最新 10 件）。

| シート | 内容 |
|---|---|
| 記録表 | 手順書の記録表と同じ「行 = 指標、列 = ラン」。実施情報、負荷（発信 / 確立 / 失敗、INVITE→200、RTP 遅れ）、PBX ホスト（channels、%CPU、nlwp、スレッド、イベント、切断理由、ログ件数）、プラグインの行、判定（手入力） |
| 時系列 | 1 秒サンプル（target / established / pending / RTP 遅れ）と監視サンプル（channels / cpu / nlwp / 追加コマンド）を時刻で結合 |
| 通話一覧 | 通話ごとの応答時間、RTP 統計、終了理由 |
| イベント | コントローラとプラグインのイベント |

「安定時」の値は、ランの後半（後半 50% のサンプル）の平均と、全体の最大。
Flatline 固有の行（ws conns、workers、net loop、検出ロック、TTS 等）は `log_patterns` / `status_command` プラグインの `report_rows` がそのまま行になる。

## 直したこと

- 検証用 FreeSWITCH のダイアルプランで、9196 / 9197 が内線直通のパターンに先に一致して 480 になっていたので順序を入れ替えた
