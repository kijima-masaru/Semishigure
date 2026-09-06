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
pytest                                   # 56 tests, PBX 不要
```

## 検証環境（FreeSWITCH / Asterisk）

`deploy/freeswitch/README.md` と `deploy/asterisk/README.md` を参照（Docker compose、WSL2 推奨）。どちらも内線 9100 と 9001〜9004、着信グループ 8001（制限 20）、ドメイン `pbx.semishigure.test` で同じ構成です。FusionPBX の管理画面で設定した FreeSWITCH は `deploy/fusionpbx/README.md`（ネイティブ導入）。

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

## FusionPBX 経由の FreeSWITCH

```bash
export SEMISHIGURE_SECRET_FUSION_9100=...   # 管理画面で自動生成された内線パスワード（9001〜9004 も同様）
semishigure call examples/fusionpbx.yaml --duration 10
semishigure load examples/fusionpbx.yaml --schedule "5:40,20:40,8:40" --duration 60 --ramp 1
```

内線と着信グループは FusionPBX の画面（Accounts → Extensions、Apps → Ring Groups）で作ります。導入と画面操作の自動化は `deploy/fusionpbx/`、実測は `docs/fusionpbx-report.md`。

## 画面

`semishigure serve` で http://127.0.0.1:8080。ガイド / 実行 / シナリオ / PBX / 結果の 5 画面（URL は `#guide` `#run` `#results/12` のように同期）。

- ガイド（セットアップガイド）: PBX プロファイルが 1 つも無いときは最初に開きます。6 ステップで負荷検証まで進めます。1. PBX に接続（プロファイルの保存、ESL / AMI のパスワードを暗号化ストアへ、接続テスト）→ 2. 内線を作る（下記のプロビジョニング。既存の内線を使う選択も可）→ 3. 内線と番号（パスワードの保存と解決の確認）→ 4. シナリオ（入力からサーバが YAML を生成）→ 5. 事前チェック（NG の項目ごとに確認点を表示）→ 6. 負荷検証（3 本で 1 分 / 段階 5→10→20 / 実行タブで手動）。入力は localStorage に保存され、パスワードの値は保存しません。

## PBX 側の設定をアプリから作る（プロビジョニング）

負荷検証に使う内線（発信側 1 つ、応答側 n 個）と着信グループを、Semishigure が PBX に作ります。パスワードは自動生成して PBX の設定に書き込み、暗号化ストアに `secret:<接頭辞>_<内線>` として保存します。変更したファイルはバックアップを取り、`unprovision` で元に戻します。ガイドの 2 番目のステップ、または CLI:

```bash
semishigure pbx provision dev-local --caller 9100 --answerers 9001,9002,9003,9004 --ring-group 8001 --group-limit 20
semishigure pbx unprovision dev-local
```

| PBX | 作られるもの | 反映 |
|---|---|---|
| FreeSWITCH（XML） | `directory/<name>/semishigure-loadtest.xml`（内線）、`dialplan/<context>/semishigure-loadtest.xml`（着信グループ = bridge の同時鳴動 + `limit` の上限、内線への直接発信）。`default.xml` に include が無ければ追加 | `reloadxml` |
| Asterisk | `semishigure-loadtest-pjsip.conf`（endpoint / auth / aor、context=semishigure）、`semishigure-loadtest-extensions.conf`（Dial の同時鳴動 + `GROUP_COUNT` の上限、X-Semishigure-Call の引き継ぎ）。`pjsip.conf` / `extensions.conf` の末尾に `#include` | `module reload res_pjsip.so`、`dialplan reload` |
| FusionPBX（プロファイルの `extra.flavor: fusionpbx`） | `v_extensions`、`v_ring_groups`、`v_ring_group_destinations`、`v_dialplans`（管理画面が作るのと同じ行）。既にある内線はそのまま使い、そのパスワードを暗号化ストアへ | キャッシュ削除、`reloadxml`。`/etc/fusionpbx/config.conf` を読める権限か、DB パスワードの `secret:` が必要 |

プロファイルの `conf_dir`（FreeSWITCH は `directory/` と `dialplan/` がある場所、Asterisk は `pjsip.conf` の場所）と、Asterisk を `-C` で起動している場合は `extra.asterisk_conf` を設定してください。作成の記録は `~/.semishigure/provision.yaml`（パスワードは含みません）。

- 実行: 手順書の順（1. 何を掛けるか → 2. どう上げるか（固定 N / 段階 / プリセット）→ 3. 記録）のフォーム、「この設定で実行します」の解決結果（host / env / 上限 / secret 名）、事前チェックの進行表示と結果。ラン中は目標 N のスライダーだけを主操作にし、状態バー（確立 / 接続中 / 失敗 / ステップ / 残り秒 / 自動減少）、指標タイル、uPlot のチャート（目標・確立・接続中・PBX channels・RTP 遅れ、ホバーで値）、通話一覧（フィルタ・ソート）、PBX ホスト（最終取得時刻と stale 表示、ログ tail）。「全通話を切る」「ランを停止」は最下部の危険ゾーンにあり確認ダイアログを経由する。終了後は結果 / xlsx / 再実行への導線。
- シナリオ: YAML 編集と保存時の検証（エラーは欄の直下、フォーカス移動）。
- PBX: プロファイルをグループ分けしたフォーム（SSH は executor=ssh のときだけ）。
- 結果: 検索・ソート・複数選択、詳細（記録表と同じ指標の表、チャート、前後のラン）、「並べて比較」（指標を列に、確立数を重ね描き）。

ライト / ダークはシステム設定に追従し、ヘッダーのボタンで切り替え。フォント（IBM Plex Sans / JetBrains Mono の欧文）と uPlot は `ui/static/vendor/` に同梱しているので CDN は不要（Vue も同様にフォールバック）。画面の検査は `tests/ui/check_ui.mjs`（Playwright + axe-core、CI の `ui` ジョブ）。改修の経緯は `docs/ui-ux-proposal.md`。

秘密情報の API（`GET /api/secrets` は名前だけ、`POST /api/secrets/{name}` は暗号化ストアへ保存、`POST /api/secrets/check` は解決できるかの真偽）は、ガイドから使うために追加したものです。値がサーバに送られるのは保存時だけで、応答に値が含まれることはありません。`serve` を 127.0.0.1 以外で待ち受ける場合は、この API があることを踏まえて TLS やアクセス制限を前段に置いてください。

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
