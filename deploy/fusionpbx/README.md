# FusionPBX 経由の FreeSWITCH（検証手順）

手順書と同じく、FusionPBX の管理画面で内線と着信グループを作り、その FreeSWITCH に Semishigure をつなぐための手順です。
このリポジトリの検証では FusionPBX 5.5 を Ubuntu 24.04 にネイティブで入れました（PostgreSQL 16、PHP 8.4、ソースビルドの FreeSWITCH 1.10.12）。

## 導入

1. FreeSWITCH を `deploy/freeswitch` の手順でビルドし、`modules.conf` に `languages/mod_lua`、`databases/mod_pgsql`、`xml_int/mod_xml_cdr`、`applications/mod_voicemail`、`applications/mod_fifo`、`applications/mod_conference`、`applications/mod_valet_parking`、`applications/mod_httapi`、`applications/mod_curl`、`formats/mod_local_stream`、`formats/mod_native_file`、`say/mod_say_en` を追加する（mod_spandsp は新しい spandsp とビルドできないため外す）
2. `sudo apt install postgresql php-cli php-pgsql php-xml php-mbstring php-curl php-gd php-sqlite3 php-zip`
3. `sudo deploy/fusionpbx/install-native.sh pbx.semishigure.test <管理者パスワード> <DB パスワード>`
   - FusionPBX の DB スキーマ、ドメイン `pbx.semishigure.test`、アプリ既定値、superadmin の `admin` を作る
   - FreeSWITCH の conf と Lua スクリプトを FusionPBX から `/etc/freeswitch` と `/usr/share/freeswitch/scripts` に置く
4. `sudo deploy/fusionpbx/run-native.sh` で管理画面（PHP 内蔵サーバ、8090）と FreeSWITCH を起動する（スクリプト末尾に出るコマンドと同じ）

nginx + php-fpm で運用する場合は FusionPBX 公式の `fusionpbx-install.sh` の nginx 設定を使ってください。検証では PHP の内蔵サーバ（`router.php`）で十分でした。

## 管理画面での設定（手順書 STEP3）

- Accounts → Extensions で 9100 と 9001〜9004 を作成（パスワードは自動生成。`limit_max` は既定 5）
- Apps → Ring Groups で `loadtest` を作成し、入口番号 8001、Strategy = Simultaneous、Destinations に 9001〜9004
- 作成後、各 Destination の Enabled が True になっていることを確認する（5.5 の画面では既定が False になることがある）

`configure-via-ui.mjs` は同じ操作を Playwright で行うスクリプトです（`node configure-via-ui.mjs <screenshot dir>`）。

内線のパスワードは管理画面（または `select extension, password from v_extensions`）で確認し、Semishigure には `secret:` 参照で渡します:

```bash
export SEMISHIGURE_SECRET_FUSION_9100='...'   # 9001..9004 も同様
semishigure call examples/fusionpbx.yaml --duration 10
```

## FusionPBX 固有の違い

| 項目 | 素の FreeSWITCH（deploy/freeswitch） | FusionPBX |
|---|---|---|
| 内線とダイアルプラン | XML ファイル | PostgreSQL から `xml_handler.lua` が配信（`mod_lua` + `mod_pgsql`） |
| 着信グループ | ダイアルプランの `bridge` | `ring_groups` Lua アプリ（INVITE→200 が 90 ms → 180 ms 程度に増える） |
| 内線ごとの上限 | なし（アプリ側の `max_calls`） | `limit_max`（既定 5）。内線への直接発信では `limit hash` で 6 本目から 486。着信グループ経由では掛からない |
| 通話制限 | `limit hash` 20 | 5.5 の着信グループ画面に通話制限の項目はない |
| 応答側の待ち受けポート | 5080 が空いている | `external` プロファイルが 5080 を使うので `answerer_port` を 5082 にする |
| SIP ドメイン | `pbx.semishigure.test` を `force-register-domain` で固定 | テナントのドメイン名。`challenge-realm=auto_to` で To のドメインが realm |
| ESL | `ClueCon` | 既定は `ClueCon`（`/etc/freeswitch/autoload_configs/event_socket.conf.xml`） |
