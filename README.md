# Semishigure（蝉時雨）

FreeSWITCH / Asterisk に SIP で接続し、同時通話の負荷テストを行う Python アプリです。
SIPp（発信）と pjsua（応答）の役割を自前の SIP / RTP 実装で担い、外部ツールを使いません。

- 発信 UAC: INVITE（ダイジェスト認証、任意ヘッダー）→ 200 → ACK → RTP → BYE / CANCEL
- 応答 UAS: 内線ごとの REGISTER（更新・再登録）、自動応答、内線ごとの同時通話上限（486）
- メディア: WAV（8kHz/16bit/mono）を G.711 に変換し、20ms ごとに送出。送出遅れを通話ごとに計測
- 秘密情報は YAML に書かず `secret:NAME` 参照（環境変数または暗号化ストア）
- 異常終了時（SIGINT / SIGTERM / 例外）に全通話の BYE と REGISTER 解除を実行

設計書: `Semishigure 設計.md`（別管理）。段階計画は設計書 7 章。**段階 1〜4 と、段階 5 の Asterisk 対応・DTMF / REFER ステップ・記録表の xlsx 出力まで完了**。段階 4 は汎用のプラグイン機構として実装し、Flatline は設定例で示しています。SIP は UDP / TCP / TLS に対応しています。同時 50 通話の容量確認は `docs/capacity.md`。

## 構成

```
semishigure/
  sip/        SIP 信号: message / sdp / auth / transport / transaction / dialog / endpoint / uac / uas
  media/      メディア境界 (base) と Python 実装 (engine): codec / wav / rtp / pump
  core/       call（通話記録・指標）, engine（配線と安全停止）, controller（負荷制御）, stats, store（SQLite）, run
  pbx/        Executor（local / ssh）, ESL クライアント, FreeSWITCH アダプタ, PBX プロファイル, 監視
  api/        FastAPI（REST + WebSocket）
  ui/static/  Vue 3 の画面（CDN。届かない環境では同梱の vendor/vue.global.prod.js に切替）
  scenario/   シナリオ YAML モデル
  plugins/    プラグイン機構（base / registry / manager）と同梱プラグイン 5 種
  secrets.py  秘密情報ストア
  cli.py      コマンドライン
deploy/freeswitch/   検証用 FreeSWITCH（Dockerfile / compose / conf）
deploy/asterisk/     検証用 Asterisk 20（Dockerfile / compose / conf テンプレート）
examples/            シナリオ例
docs/                判断記録・段階レポート
tests/               単体テストと UAC⇄UAS ループバックテスト（PBX 不要）
```

`semishigure.sip` は `semishigure.media.base` のインターフェースしか見ません。RTP 送出を Go / Rust に置き換えるときは
`MediaEngine` / `MediaSession` を実装したクラスを差し込むだけです。

## セットアップ

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                                   # 24 tests, PBX 不要
```

## 検証環境（FreeSWITCH / Asterisk）

`deploy/freeswitch/README.md` と `deploy/asterisk/README.md` を参照（Docker compose、WSL2 推奨）。どちらも内線 9100 と 9001〜9004、着信グループ 8001（制限 20）、ドメイン `pbx.semishigure.test` で同じ構成です。

## 段階 1 の確認コマンド

```bash
semishigure audio synth audio/speech_en.wav --seconds 60 --pitch 160 --seed 1
semishigure audio synth audio/speech_ja.wav --seconds 60 --pitch 230 --seed 2
export SEMISHIGURE_SECRET_EXT=<内線パスワード>     # または: semishigure secret set ext
semishigure -v call examples/dev-freeswitch.yaml --duration 20 --record-rx runs/rx --report runs/report.json
```

出力例と実測は `docs/stage1-report.md`。

## 段階 2 の使い方（負荷制御）

```bash
# ヘッドレス: N を 5 → 20 → 8 と 40 秒ずつ変える（通話長 60 秒、発信 1 本/秒）
semishigure load examples/dev-freeswitch.yaml --schedule "5:40,20:40,8:40" --duration 60 --ramp 1
# シナリオのプリセット A（5 → 10 → 20、各 call_duration）
semishigure load examples/dev-freeswitch.yaml --preset A
# 画面: http://127.0.0.1:8080  スライダー / ±1 ±5 / バースト / 一時停止 / 全切断 / スケジュール
semishigure serve --scenarios examples
```

ランは `~/.semishigure/runs.sqlite3` に保存されます（`SEMISHIGURE_HOME` で変更）。実測は `docs/stage2-report.md`。

## 段階 3 の使い方（PBX 接続と監視）

```bash
semishigure pbx init                      # ~/.semishigure/pbx_profiles.yaml の雛形を作る
export SEMISHIGURE_SECRET_ESL=<ESL パスワード>   # または: semishigure secret set esl
semishigure pbx list
semishigure pbx test dev-local            # 接続・上限値・チャネル数・プロセス・ログ末尾
semishigure load examples/dev-freeswitch.yaml --pbx-profile dev-ssh --schedule "5:60,20:60,8:60"
```

シナリオの `pbx_profile:` にプロファイル名を書くと、実行中に PBX ホストの channels / %CPU / スレッド / ログ / ESL イベントを 2 秒周期で取り、画面のグラフとログテールに出します。
SSH の場合は鍵認証のみで、ESL はポートフォワードで届きます。実測は `docs/stage3-report.md`。

## プラグイン（段階 4）

シナリオの `plugins:` にサービス固有の処理を足します。同梱は `log_patterns`（ログの集計）、`status_command`（status 出力の抽出）、
`conf_override`（設定の一時変更と復元）、`ws_hook`（通話ごとの WebSocket 連携）、`webhook`（HTTP 通知）。
独自クラスは `module: pkg.mod:Class` で読み込みます。書き方は `docs/plugins.md`、設定例は `examples/plugins-example.yaml`。

## トランスポート（UDP / TCP / TLS）

```yaml
pbx:
  transport: tls          # udp | tcp | tls
  sip_port: 5061
  tls_verify: false       # 自己署名の検証用 PBX。本番は true と tls_ca
```

PBX プロファイルでは `sip_transport` / `sip_tls_port` / `tls_verify` / `tls_ca`。TLS では応答側の待ち受けに自己署名証明書を自動生成します（`~/.semishigure/tls/`。`pbx.tls_cert` / `tls_key` で差し替え）。
検証用 FreeSWITCH / Asterisk は起動時に証明書を生成し、5061 で TLS を待ち受けます。RTP は UDP のままです（SRTP は未対応）。

## 通話ステップと記録表

```yaml
caller:
  steps:
    - hold: 30s
    - dtmf: "*4"        # RFC 2833
    - hold: 10s
    - refer: "9004"     # ブラインド転送
    - bye
```

```bash
semishigure runs                             # 保存したランの一覧
semishigure report --runs 3,4,5 -o report.xlsx   # 記録表（行 = 指標、列 = ラン）と時系列 / 通話一覧 / イベント
```

画面の Past runs からもダウンロードできます。詳細は `docs/stage5-steps-report.md`。

## Asterisk

```bash
semishigure call examples/dev-asterisk.yaml --duration 10                 # 1 通話の疎通
semishigure load examples/dev-asterisk.yaml --pbx-profile asterisk-local --schedule "5:60,20:60,8:60"
```

`type: asterisk` のプロファイルでは AMI（`Command` アクションとイベント）を使い、使えなければ `asterisk -rx` に落ちます。実測は `docs/stage5-asterisk-report.md`。

## 画面

`semishigure serve` で http://127.0.0.1:8080。実行（スライダー、事前チェック、監視、プラグイン）、シナリオ（YAML 編集）、PBX（プロファイル編集と Test）、結果（ラン一覧、詳細、記録表 xlsx）の 4 画面。

## 開発

```bash
pip install -e ".[dev]" httpx
ruff check semishigure tests
pytest -q            # 51 件、PBX 不要（CI と同じ）
```

## 制約（共有事項 6 節）

- SIPp / pjsua / PJSIP / ESL ライブラリは使わない。依存は MIT / Apache-2.0 / BSD / EPL-2.0 / LGPL のみ
  - 実行時: pyyaml (MIT), cryptography (Apache-2.0 / BSD)。段階 2 以降: fastapi (MIT), uvicorn (BSD), websockets (BSD), asyncssh (EPL-2.0), openpyxl (MIT)
- 内線パスワードや SSH 鍵パスフレーズは YAML やコードに書かない
- `prod` 環境タグでは同時数上限の強制と実行前確認（段階 2 で実装）
