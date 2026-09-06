<img src="semishigure/ui/static/icon.png" width="96" alt="" align="left" style="margin-right:16px">

# Semishigure（蝉時雨）

PBX（FreeSWITCH / Asterisk / FusionPBX）に対する同時通話の負荷検証ツールです。SIP / RTP のスタックを内蔵し、発信側と応答側の両方を自分で演じます。SIPp や pjsua は使いません。目標同時数 N を画面から動かしながら、応答時間・RTP の送出遅れ・PBX 側の channels / CPU / ログを記録し、記録表（xlsx）に落とします。

## 動作要件

- Python 3.12（Windows は WSL2 の Ubuntu を推奨。Linux / macOS でも可）
- 負荷を掛ける PBX に、このマシンから SIP（UDP 5060 など）と RTP が届くこと
- 監視をする場合は PBX の ESL（FreeSWITCH）/ AMI（Asterisk）、または PBX ホストへの SSH（鍵認証）

Docker は不要です。対象は既に稼働している PBX で、このアプリのために PBX を立てる必要はありません。

## Windows デスクトップ版

GitHub の Releases から `Semishigure-<版>-setup.exe` をダウンロードして実行します。Python の別途インストールは不要です（3.12 を同梱）。

- スタートメニューの「Semishigure」でアプリ窓が開きます（Microsoft Edge か Google Chrome のアプリ モード。どちらも無い場合は既定のブラウザで開きます）。窓を閉じると進行中のランを止めて終了します
- コマンド `semishigure`（CLI）と `semishigure-desktop`（コンソール版: サーバのログを表示しながら既定ブラウザで画面を開く）も同梱されます。インストール先の `bin` フォルダを PATH に足すか、フルパスで実行してください
- データは `C:\Users\<名前>\.semishigure\` に置かれます（プロファイル、シナリオ、暗号化ストア、結果、`desktop.log`）
- 待ち受けは 127.0.0.1 だけです。他の PC からは使えません

**更新**: ヘッダー右の更新アイコン（下向き矢印）を押すと最新版を確認します。起動時にも確認し、新しい版があれば案内が出ます。「今すぐ更新する」を選ぶとインストーラをダウンロードして検証し、アプリをいったん閉じて更新、終わると自動で再び開きます（実行中のランがあるときは先に止めてください）。

更新の途中で止まった場合は `C:\Users\<名前>\.semishigure\updates\update.log` に経過が残ります（デバッグ タブにも出ます）。

**不具合の報告**: 「デバッグ」タブの「すべてコピー」で、アプリのログと環境情報を貼り付けられる形で取り込めます。

起動しないときは次を見てください。

1. `C:\Users\<名前>\.semishigure\desktop.log`: サーバの起動と、アプリ窓が開けなかった理由が記録されます。起動時の例外はダイアログでも表示します
2. `%APPDATA%\Semishigure.launch.pyw.log`（`C:\Users\<名前>\AppData\Roaming\`）: 起動スクリプト自体が落ちたときの記録です
3. どちらも無いときは、コマンド プロンプトで `"<インストール先>\bin\semishigure-desktop.exe"` を実行すると画面にエラーが出ます。インストール先は通常 `C:\Users\<名前>\AppData\Local\Programs\Semishigure`（全ユーザー向けに入れた場合は `C:\Program Files\Semishigure`）です

負荷を掛ける PBX へは、Windows から SIP（UDP）と RTP が届く必要があります。Windows Defender ファイアウォールの許可を求められたら「プライベート ネットワーク」で許可してください。RTP の 20 ms 送出は Python 3.12 の高分解能タイマーで動きますが、大きな同時数（30 本以上）を掛ける場合は WSL2 か Linux での実行を推奨します。

## インストール

```bash
git clone https://github.com/kijima-masaru/Semishigure.git
cd Semishigure
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e .
semishigure serve                       # http://127.0.0.1:8080
```

設定とデータは `~/.semishigure/`（`SEMISHIGURE_HOME` で変更可）に置かれます: `pbx_profiles.yaml`（PBX プロファイル）、`scenarios/`（シナリオ）、`secrets.enc` と `master.key`（暗号化ストア）、`runs.sqlite3`（結果）、`provision.yaml`（PBX に作った設定の記録）。

## 使い方（実際の PBX に対して）

画面の「ガイド」タブが 6 ステップで案内します。PBX プロファイルが 1 つも無いときは最初にこのタブが開きます。

1. **PBX に接続**: SIP アドレスとドメイン、ESL / AMI の接続先とパスワード（暗号化ストアへ）を登録し、接続テストで確認
2. **内線を作る**: 負荷検証用の内線と着信グループをアプリから PBX に作成（下記のプロビジョニング）。既存の内線を使うこともできます
3. **内線と番号**: 発信側 / 応答側の内線とパスワードの解決を確認
4. **シナリオ**: 入力からシナリオ YAML を生成
5. **事前チェック**: REGISTER・1 本の発信と応答・両方向の RTP・BYE・監視を確認。NG の項目ごとに確認点を表示
6. **負荷検証**: 「3 本で 1 分」「段階 5 → 10 → 20」などから選んで開始し、実行タブへ

実行タブでは目標 N をスライダーで動かし、状態バー（確立 / 接続中 / 失敗 / 自動減少）、指標タイル、チャート、通話一覧、PBX ホストの監視を見ながら進めます。終了後は結果タブで記録表と同じ指標を確認し、xlsx を出力します。複数のランは「並べて比較」できます。

同じことはコマンドでもできます:

```bash
semishigure pbx test my-pbx                                   # 接続テスト
semishigure pbx provision my-pbx --caller 9100 --answerers 9001,9002,9003,9004 --ring-group 8001
semishigure call ~/.semishigure/scenarios/my-loadtest.yaml --duration 10        # 1 通話の疎通
semishigure load ~/.semishigure/scenarios/my-loadtest.yaml --schedule "5:180,10:180,20:180"
semishigure runs; semishigure report --ids 1,2,3 -o report.xlsx
semishigure secret set ext_9100                                # パスワードを暗号化ストアへ
```

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

## プラグイン

シナリオの `plugins:` にサービス固有の処理を足します。同梱は `log_patterns`（ログの集計）、`status_command`（status 出力の抽出）、
`conf_override`（設定の一時変更と復元）、`ws_hook`（通話ごとの WebSocket 連携）、`webhook`（HTTP 通知）。
独自クラスは `module: pkg.mod:Class` で読み込みます。書き方は `docs/plugins.md`、設定例は `examples/plugins-example.yaml`。

## トランスポート（UDP / TCP / TLS）

```yaml
pbx:
  transport: tls          # udp | tcp | tls
  sip_port: 5061
  tls_verify: false       # PBX の証明書が自己署名なら false。CA 発行なら true と tls_ca
```

PBX プロファイルでは `sip_transport` / `sip_tls_port` / `tls_verify` / `tls_ca`。TLS では応答側の待ち受けに自己署名証明書を自動生成します（`~/.semishigure/tls/`。`pbx.tls_cert` / `tls_key` で差し替え）。RTP は UDP のままです（SRTP は未対応）。

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

## 開発者向け

- テスト: `pip install -e ".[dev]"` の後 `ruff check semishigure tests` と `pytest`（PBX 不要のループバックテスト）。画面は `tests/ui/check_ui.mjs`（Playwright + axe-core）。CI は `.github/workflows/ci.yml`
- 検証用の PBX（FreeSWITCH / Asterisk / FusionPBX のネイティブ起動と Docker）、実測レポート、設計上の判断: `docs/development.md`、`docs/decisions.md`、`docs/*-report.md`
- サンプルのシナリオとプロファイル: `examples/`（検証用 PBX 向け）。リリース物（wheel）に含まれるのは `semishigure` パッケージだけで、`deploy/` `docs/` `examples/` `tests/` は含まれません

## 制約（共有事項 6 節）

- SIPp / pjsua / PJSIP / ESL ライブラリは使わない。依存は MIT / Apache-2.0 / BSD / EPL-2.0 / LGPL のみ
  - 実行時: pyyaml (MIT), cryptography (Apache-2.0 / BSD)。段階 2 以降: fastapi (MIT), uvicorn (BSD), websockets (BSD), asyncssh (EPL-2.0), openpyxl (MIT)
- 内線パスワードや SSH 鍵パスフレーズは YAML やコードに書かない
- `prod` 環境タグでは同時数上限の強制と実行前確認（段階 2 で実装）
