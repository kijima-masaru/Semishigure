# 段階 4 レポート（プラグイン機構）

作成日: 2026-09-06
方針: Flatline 専用ではなく、本体にフックを持たせた汎用のプラグイン機構として実装した。Flatline の負荷テストは同梱プラグインの組み合わせ（設定例）で表現する。

## 実装

- `semishigure/plugins/base.py`: `Plugin` 基底クラスと `PluginContext`（adapter / secrets / monitor / PBX 側の通話対応付け）
- `semishigure/plugins/registry.py`: 同梱プラグインの名前解決と `module: pkg.mod:Class` の外部読み込み、`enabled: false`
- `semishigure/plugins/manager.py`: フックの一斉呼び出し。1 プラグインの例外はそのプラグインの `errors` に閉じ込める
- 同梱プラグイン: `log_patterns` / `status_command` / `conf_override` / `ws_hook` / `webhook`
- 本体側: `Run` がプラグインを読み込み、`pre_run` → 発信 → `post_run`。`post_run` は `SipEngine.shutdown_hooks` 経由で SIGINT / SIGTERM / 例外でも実行。`Monitor` の周期とログ行をプラグインに流す。通話の確立 / 終了をフックに通知。画面に Plugins パネル、`semishigure load` の末尾に plugin rows、`semishigure plugins` で一覧

## 検証

### 単体テスト（6 件、PBX 不要）

- 読み込み（同梱 / 外部 / 無効化 / 不正な指定）
- 例外の隔離（壊れたプラグインがあっても他のプラグインとランは続く）
- `log_patterns`: collect_metrics.sh のログ行（待機モード解除 → 言語検出ロック、再生開始の秒数、TTFB、429）で件数 / 区間 / 数値を集計
- `conf_override`: XML の `<param>` 置換（属性順不同）と正規表現置換、ローカル実行でのバックアップ → 適用 → 復元 → reload
- `status_command`: 正規表現の項目と `key: value` の自動抽出、最大値の追跡
- `ws_hook`: プロセス内の WebSocket サーバに対して、チャネル変数の取得 → 接続 → 条件メッセージ待ち → JSON 送信 → 通話終了で切断

### FreeSWITCH での実行（同時 3 通話、監視つき）

```
semishigure load plugins-verify.yaml --target 3 --ramp 2 --duration 60 --time 14
```

| プラグイン | 結果 |
|---|---|
| `log_patterns` | `[WARNING]` 7 件、SIP auth challenge 7 件、発信チャネルの New Channel → Close Channel の区間 平均 13.04 秒 / 最大 13.54 秒（uuid で対応付け） |
| `status_command`（`status`） | sessions peak 43、idle cpu 96.2 / 97.73 |
| `conf_override` | `loglevel` を `debug` に変更し、終了時に復元（差分なし、バックアップ削除、reload 2 回） |

### 異常終了時の復元

同じランを起動 9 秒後に SIGINT で止めた。設定ファイルは変更中（`value="debug"`）だったが、停止後は元と同一で、reload も 2 回実行され、PBX の通話は 0 本になった。

## Flatline を組む場合（設定例 `examples/plugins-example.yaml`）

| 手順書の作業 | プラグイン |
|---|---|
| loadtest_operator.py（chat_uuid 取得 → WSS 接続 → 9001 待ち → openChat） | `ws_hook` |
| collect_metrics.sh（待機解除 → ロック、受信 → 再生、TTFB、429、reconnect） | `log_patterns` |
| `flatline_interpreter status`（ws conns、workers、net loop） | `status_command` |
| パターン C の conf 変更と復元 | `conf_override` |

実環境（Flatline 一式、ChatAPI、Deepgram）での確認は未実施。ログと status の書式は手順書と collect_metrics.sh の記載に合わせてあるが、実際の出力に合わせて正規表現の調整が必要になる可能性がある。
