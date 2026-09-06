# FusionPBX 経由の FreeSWITCH 検証レポート

作成日: 2026-09-06
目的: 手順書（負荷テスト手順書）が前提にしている「FusionPBX の管理画面で内線と着信グループを作った FreeSWITCH」に対して、素の FreeSWITCH・Asterisk と同じシナリオで発信・応答・負荷制御・監視ができることを確認する。

## 環境

| 項目 | 内容 |
|---|---|
| FusionPBX | 5.5（GitHub `fusionpbx/fusionpbx` の 5.5 ブランチ）を Ubuntu 24.04 にネイティブ導入。PostgreSQL 16、PHP 8.4 CLI の内蔵サーバ（`deploy/fusionpbx/router.php`）で http://127.0.0.1:8090 |
| FreeSWITCH | 1.10.12 ソースビルド。FusionPBX 用に `mod_lua` / `mod_pgsql` / `mod_xml_cdr` / `mod_voicemail` / `mod_fifo` / `mod_conference` / `mod_valet_parking` / `mod_httapi` / `mod_curl` / `mod_local_stream` / `mod_native_file` / `mod_say_en` を追加してビルド（`mod_spandsp` は新しい spandsp とビルドできず除外）。conf は FusionPBX のもの（`/etc/freeswitch`）。internal プロファイル 192.0.2.2:5060、external 5080 |
| 導入手順 | `deploy/fusionpbx/install-native.sh`（DB・スキーマ・ドメイン・既定値・superadmin）、`deploy/fusionpbx/run-native.sh`（起動） |
| 管理画面での設定 | Accounts → Extensions で 9100、9001〜9004（パスワード自動生成、`limit_max` 5）。Apps → Ring Groups で `loadtest` 8001、Strategy = Simultaneous、Destinations 9001〜9004。`deploy/fusionpbx/configure-via-ui.mjs`（Playwright）で同じ操作を自動化し、スクリーンショットで確認 |
| アプリ側 | `examples/fusionpbx.yaml`、プロファイル `fusionpbx-local`（`examples/pbx_profiles.example.yaml`）。パスワードは `secret:fusion_9100` などの参照で、値は環境変数 `SEMISHIGURE_SECRET_FUSION_*` から渡した |

## 確認したこと

| 項目 | 結果 |
|---|---|
| REGISTER（9001〜9004、FusionPBX の `xml_handler.lua` が PostgreSQL から配信する directory に対して） | 200 |
| INVITE（9100 → 8001、401 → 認証つき再送） | 180 が 43.9 ms、200 が 177.5 ms（1 通話） |
| 着信グループ（`ring_groups` Lua アプリ、Simultaneous） | 9001〜9004 が同時に鳴り、1 本が応答、残りは CANCEL（PBX 側の切断理由 `LOSE_RACE`） |
| 対応付けヘッダー | INVITE の `X-Semishigure-Call` が B レグにそのまま渡る（素の FreeSWITCH と同じ） |
| RTP | 10 秒で送信 401 / 受信 398 パケット、欠落 0 |
| PBX 監視（ESL、`fs_cli`） | `show channels count`、プロセス / スレッドの %CPU、`freeswitch.log`（UUID 付き行）、CHANNEL_* イベントと切断理由 |
| `limit_max`（FusionPBX 固有） | 9001 へ直接発信して目標 8 本にすると 5 本が確立し、6〜8 本目は PBX が 486（`switch_limit` の `pbx.semishigure.test_9001 max:5`、切断理由 `USER_BUSY`）。着信グループ経由では掛からない |

## 負荷検証（5 → 20 → 8、通話長 60 秒、発信 1 本/秒）

```
semishigure load examples/fusionpbx.yaml --schedule "5:40,20:40,8:40" --duration 60 --ramp 1
```

| 指標 | 値 |
|---|---|
| 発信 / 確立 / 失敗 | 28 / 28 / 0 |
| INVITE→180 p50 / p95 | 40.0 / 44.6 ms |
| INVITE→200 p50 / p95 / max | 176.9 / 197.6 / 206.5 ms |
| 応答内線の分布 | 9001〜9004 に 7 本ずつ |
| 応答側の 486（`max_calls` 5 超過） | 21 |
| RTP 送出遅れ最大 / 5 ms 超 | 17.66 ms / 42 件（tx 61,239 / rx 61,188、欠落 0） |
| FreeSWITCH の channels（N=20） | 最大 43（= 通話数 × 2 + 鳴動中の B レグ） |
| FreeSWITCH の %CPU（区間値） | 平均 5%、ピーク 24.5%（20 本の同時鳴動時） |
| FreeSWITCH の nlwp（待機 / N=20） | 25 / 68 |
| ESL イベント | CHANNEL_CREATE 140、CHANNEL_ANSWER 56、CHANNEL_HANGUP_COMPLETE 140 |
| 切断理由 | NORMAL_CLEARING 56、LOSE_RACE 63、USER_BUSY 21 |
| ログ（レベル別） | NOTICE 1,519、INFO 224、DEBUG 8,164 |
| プラグイン `log_patterns` | `ring_groups` を含む行 56、A レグの寿命（New Channel → Close Channel）平均 43.9 秒 / 最大 60.3 秒 |

## 素の FreeSWITCH / Asterisk との比較

| 項目 | 素の FreeSWITCH（段階 2） | Asterisk 20 | FusionPBX 5.5 |
|---|---|---|---|
| INVITE→200 p50 | 約 92 ms | 62.0 ms | 176.9 ms |
| INVITE→180 p50 | 約 50 ms | 10.4 ms | 40.0 ms |
| 着信グループの実装 | ダイアルプラン XML の `bridge` | `Dial()` | `ring_groups` Lua（PostgreSQL 参照） |
| 失敗 | 0 | 0 | 0 |

INVITE→200 が 90 ms ほど長いのは、FusionPBX がダイアルプランと着信グループを Lua と PostgreSQL の問い合わせで解決する時間で、Semishigure 側の差はない（応答側の同時鳴動の窓 50 ms は共通）。
RTP の送出遅れが 5 ms を超えた 42 件は 20 本立ち上げ時の CPU ピークと重なる区間で、欠落は 0。手順書の記録項目（応答時間、RTP の遅れ、channels、CPU、スレッド数、ログ、切断理由）はすべて取れている。

## FusionPBX 固有で気づいたこと

- 5.5 の着信グループ画面で追加した Destination が `destination_enabled = false` で保存されることがあり、鳴らない。保存後に Enabled を確認する（検証では SQL で `true` に更新した）
- 内線のパスワードは画面で自動生成される。アプリには `secret:` 参照で渡し、YAML には書かない
- `external` プロファイルが 5080 を bind するため、応答側の待ち受けは 5082 にした
- FreeSWITCH の conf と Lua スクリプトは FusionPBX 側にあり、`/etc/freeswitch` と `/usr/share/freeswitch/scripts` に置いてから `upgrade.php --defaults` を実行する必要がある（無いと `copy_languages` で止まる）
- ログは行頭に通話 UUID が付くので、`log_patterns` の `key` を `^([0-9a-f-]{36})` にすると通話ごとのタイマーが取れる

## 検証できていないこと

- nginx + php-fpm での FusionPBX 運用（検証は PHP 内蔵サーバ）。Semishigure が接続するのは FreeSWITCH なので、管理画面のサーバ構成は結果に影響しない
- FusionPBX の Docker イメージ（この環境ではレジストリからの取得ができない）
- Flatline 一式（言語検出、TTS など）。手順書の X-LANG / X-ENABLE-* ヘッダーは同じ形で送っているが、受け側の処理は無い
