# TLS / TCP トランスポートと画面の仕上げのレポート

作成日: 2026-09-06

## SIP over TCP / TLS

`StreamTransport` を追加した。待ち受け 1 つ（Contact のポート）と宛先ごとのオンデマンド接続で、応答は要求が届いた接続に返す。
Via と Contact は `TCP` / `TLS`（`;transport=tls`）になり、RTP は UDP のまま。

| 確認 | FreeSWITCH（5061 TLS / 5060 TCP） | Asterisk（5091 TLS / 5090 TCP） |
|---|---|---|
| REGISTER | 200（TLS 8.0 ms / TCP 3.2 ms） | 200（TLS 9.0 ms / TCP 2.5 ms） |
| INVITE→200 | 91.9 ms / 91.2 ms | 67.2 ms / 60.0 ms |
| RTP 双方向 | 250 / 250 パケット、欠落 0 | 250 / 250 パケット、欠落 0 |
| PBX からの BYE | 応答側の待ち受けへ新規接続で到達 | 同左 |

検証用 PBX は起動時に自己署名証明書を生成する（FreeSWITCH: `gen-certs.sh` → `agent.pem` / `cafile.pem`、Asterisk: `render-conf.sh` → `asterisk.crt` / `.key`）。
Semishigure 側の待ち受け用証明書は `~/.semishigure/tls/` に自動生成（EC P-256、SAN にローカル IP）。
PBX 証明書の検証は既定で有効で、検証用 PBX にだけ `tls_verify: false` を使う。

ループバックの単体テストで、ストリームの分割・結合（任意の 7 バイト単位）と、TCP / TLS それぞれの通話（INVITE → RTP → 相手からの BYE）を検証している。

## 画面の仕上げ（設計 3.7）

| 画面 | 内容 |
|---|---|
| 実行 | 従来の実行画面に **事前チェック** を追加。REGISTER、PBX 監視の接続、1 本の発信 → 応答、両端の RTP（パケット数と音量）、BYE、ESL / AMI イベント、プラグインのエラーを ✔ / ✖ で表示 |
| シナリオ | YAML の編集（保存時に検証）、選択中をコピーして新規作成、削除、解釈結果（接続先、ヘッダー、ステップ、内線、負荷、プラグイン、監視）の表示 |
| PBX | プロファイルのフォーム（項目はサーバの定義から自動生成）、保存、削除、Test。パスワード欄は `secret:NAME` 参照のみ受け付ける |
| 結果 | ラン一覧、チェックしたランの記録表 xlsx、ランの詳細（集計タイル、target / established / PBX channels のグラフ、プラグインの行、イベント） |

Playwright で 4 画面を操作し、事前チェックが 10 秒で OK になること、YAML の読み込み、プロファイルのフォーム表示、詳細グラフを確認した。

## CI

`.github/workflows/ci.yml` で push / PR ごとに `ruff` と `pytest`（51 件、PBX 不要）を実行する。
