# プラグイン機構

Semishigure 本体は PBX とサービスに依存しない負荷テスト（発信・応答・負荷制御・PBX 監視）だけを持ち、
サービス固有の処理はプラグインとして後から足します。シナリオ YAML の `plugins:` に書いた順に読み込まれます。

```yaml
plugins:
  log_patterns:                 # 同梱プラグイン名
    counters: [...]
  my_service:                   # 任意の名前 + module で外部クラス
    module: mypkg.semishigure_plugin:MyServicePlugin
    option: value
  webhook:
    enabled: false              # 設定を残したまま無効化
```

## フック

`semishigure.plugins.base.Plugin` を継承し、必要なものだけ実装します。すべて省略可能です。

| フック | 呼ばれるタイミング | 用途 |
|---|---|---|
| `pre_run()` | 最初の発信の前。例外を投げるとランを中止 | 設定の一時変更、外部サービスの準備 |
| `post_run()` | 最後の通話の後。**異常終了（Ctrl-C、例外）でも必ず呼ばれる** | 復元、接続の後始末 |
| `on_call_established(call)` | 発信側の通話が確立したとき | 通話ごとの外部連携（WebSocket、API 呼び出し） |
| `on_call_ended(call)` | 確立していた通話が終わったとき | 接続を閉じる |
| `collect_metrics(adapter)` | 監視周期（既定 2 秒）ごと。戻り値の dict は `プラグイン名.キー` で監視の時系列に入る | status 出力の解析、独自指標 |
| `parse_log_line(line)` | PBX ログの 1 行ごと（同期） | ログの件数・区間時間の集計 |
| `snapshot()` | 画面と JSON 出力 | 現在の状態 |
| `report_rows()` | ランの集計と記録表 | `(ラベル, 値)` の行 |

`self.ctx`（`PluginContext`）から `adapter`（PBX アダプタ。プロファイルなしなら None）、`secrets`、`monitor`、
`pbx_call(call_id)`（ESL / AMI で対応付いた PBX 側の uuid や切断理由）が使えます。
1 つのプラグインの例外はそのプラグインの `errors` に記録され、ランは続きます。

```python
from semishigure.plugins.base import Plugin

class MyServicePlugin(Plugin):
    """一行目が説明として表示される"""

    async def on_call_established(self, call):
        self.counters["calls"] += 1

    async def collect_metrics(self, adapter):
        out = await adapter.api("my_module status")
        return {"queue": int(out.split()[-1])}
```

## 同梱プラグイン

| 名前 | 内容 | 主な設定 |
|---|---|---|
| `log_patterns` | ログ行の件数、開始行から終了行までの秒数（キーで対応付け）、行中の数値の件数 / 平均 / 最大 | `counters` / `timers` / `numbers` / `key` |
| `status_command` | 周期的に api または shell コマンドを実行し、正規表現や `key: value` で複数の値を取る | `api` または `cmd` / `fields` / `keyvalue` / `first_line_as` |
| `conf_override` | PBX ホストの設定ファイルを一時変更（XML の `<param name value>` または正規表現置換）。バックアップを取り、終了時に復元して reload | `files[].path` / `params` / `replace` / `reload` |
| `ws_hook` | 通話ごとに WebSocket へ接続し、条件に合うメッセージを待ってから JSON を送る。チャネル変数と INVITE ヘッダーをプレースホルダで使える | `url` / `variables` / `wait_for` / `send` / `headers` |
| `webhook` | ラン開始 / 終了、通話確立 / 終了を HTTP POST | `url` / `events` / `headers` |

設定例は `examples/plugins-example.yaml`。Flatline の負荷テスト（手順書）は、この 5 つの組み合わせで表現できます:
`loadtest_operator.py` → `ws_hook`、`collect_metrics.sh` → `log_patterns`、`flatline_interpreter status` → `status_command`、
パターン C の conf 変更 → `conf_override`。

## 秘密情報

プラグイン設定でも値を直接書かず `{secret:NAME}`（ヘッダーや URL のプレースホルダ）または `secret:NAME` 参照を使います。

## CLI と画面

- `semishigure plugins` で同梱プラグインの一覧
- 画面の Plugins パネルに各プラグインの `snapshot()`、監視の最新サンプルにプラグインの指標
- `semishigure load` の最後に `plugin rows` として `report_rows()` を表示
