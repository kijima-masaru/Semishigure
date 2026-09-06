# Semishigure 検証用 Asterisk

設計書 9.1 と同じ構成を Asterisk 20（Ubuntu 24.04 のパッケージ、chan_pjsip）で用意します。

| 設計 9.1 の要求 | この環境の値 |
|---|---|
| SIP ドメイン | `pbx.semishigure.test`（`SEMI_AST_DOMAIN`。auth の realm と from_domain） |
| SIP トランスポート | UDP 5060（`SEMI_AST_SIP_IP` / `SEMI_AST_SIP_PORT`。ネイティブ実行で FreeSWITCH と同居する場合は 5090 など） |
| RTP ポート範囲 | 16384〜32768（`SEMI_AST_RTP_START` / `SEMI_AST_RTP_END`） |
| 発信用内線 | 9100 |
| 応答用内線 | 9001〜9004（`max_contacts=1`。同時通話数の制限はアプリ側の `max_calls`） |
| 着信グループ | 入口番号 8001、`Dial(PJSIP/9001&9002&9003&9004)` の同時鳴動。通話制限 20 は `GROUP()` / `GROUP_COUNT()` で判定し、超過は 486 |
| 上限値 | `maxcalls` 100（`SEMI_AST_MAXCALLS`） |
| 内線パスワード | `SEMI_AST_EXT_PASSWORD`（既定 `semishigure-dev`、開発専用） |
| AMI | 127.0.0.1:5038、ユーザー `semishigure`、パスワード `SEMI_AST_AMI_PASSWORD`。`channelvars=SEMI_CALL` で通話の対応付け用変数をイベントに載せる |
| テスト用番号 | 9196 エコー、9197 ミリワットトーン、`9xxx` 内線直通 |

発信側の `X-Semishigure-Call` と `X-LANG` ヘッダーは、ダイアルプランの pre-dial ハンドラで B レグにコピーします（FreeSWITCH は自動でコピーしますが Asterisk はしないため）。

## 起動方法

### Docker（WSL2 推奨）

```bash
cd deploy/asterisk
docker compose up -d --build
docker exec semishigure-ast asterisk -rx "pjsip show endpoints"
```

### ネイティブ（Ubuntu / Debian の apt パッケージ）

```bash
sudo apt install asterisk asterisk-modules asterisk-config
SEMI_AST_SIP_IP=127.0.0.1 SEMI_AST_SIP_PORT=5090 deploy/asterisk/run-native.sh
# 別ターミナルから
asterisk -C deploy/asterisk/runtime/etc/asterisk.conf -rx "pjsip show endpoints"
```

`run-native.sh` は `conf/` のテンプレート（`__X__`）を `runtime/etc` に展開し、`asterisk -f -C` で起動します。
ソースビルドの Asterisk では `SEMI_AST_DATADIR=/var/lib/asterisk` を指定してください。

## Semishigure 側の設定

`examples/pbx_profiles.example.yaml` の `asterisk-local` を参考に、`type: asterisk` のプロファイルを登録します。
API コマンドは AMI の `Command` アクション、使えないときは `asterisk -rx` です。`-C` で起動している場合は `extra.asterisk_conf` にパスを書きます。
シナリオは `examples/dev-asterisk.yaml`。

## 段階 3 相当の SSH

`SEMI_AST_SSH=1` と `SEMI_AST_SSH_PUBKEY` で sshd（2222、ユーザー `semi`、鍵認証のみ）が起動します。

## TLS

起動時に自己署名証明書を生成し、5061（`SEMI_FS_TLS_PORT` / `SEMI_AST_TLS_PORT`）で SIP over TLS を待ち受けます。証明書の検証はしません（検証用）。
Semishigure 側はプロファイルの `sip_transport: tls` と `tls_verify: false`、または シナリオの `pbx.transport: tls` で接続します。TCP は UDP と同じポートです。
