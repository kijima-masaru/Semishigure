# 段階 3 レポート（PBX 接続と監視）

作成日: 2026-09-06
環境: 段階 1〜2 と同じコンテナ。FreeSWITCH はネイティブ起動、ESL は 127.0.0.1:8021。
コンテナ内に sshd（127.0.0.1:2222、ユーザー `semi`、鍵認証のみ）を立て、`LocalExecutor`（同一ホスト）と `SshExecutor`（SSH 経由、ESL はポートフォワード）の両方で確認した。

## 完了条件の確認

「手順書 STEP11 の値が画面に出る」

monitor.sh が表示する値のうち汎用のもの（`show channels count`、freeswitch の %CPU / %MEM / nlwp、スレッド別 CPU、ログ）を
2 秒周期で取得し、CLI と画面の両方に出した。Flatline 固有の `flatline_interpreter status` と main.lua の START/END 差分は段階 4 のプラグインで追加する。

| STEP11 の項目 | 取得方法 | 画面 |
|---|---|---|
| show channels count | ESL `api show channels count`（ESL 不可なら fs_cli） | タイル + グラフ |
| ps -o pcpu,pmem,nlwp | `ps` に加えて `/proc/<pid>/stat` の jiffies 差分から区間 %CPU を算出（ps の値は起動からの平均で負荷が見えない） | タイル + グラフ |
| ps -L（スレッド別） | `/proc/<pid>/task/*/stat` の差分。名前ごとの本数と合計 %CPU、上位スレッド | 表 |
| ログ | `tail -F` をストリーム。末尾 80 行と WARNING / ERR / CRIT の件数 | ログテール |
| 通話イベント | ESL で CHANNEL_CREATE / ANSWER / HANGUP_COMPLETE を購読。`X-Semishigure-Call` で自通話に対応付け、切断理由を集計 | タイル |
| 追加の監視コマンド | シナリオの `monitor.commands`（api / shell、number / last / regex / text で解析） | タイル |

## 実測（SSH 経由、N=6 → 12、通話長 60 秒）

```
semishigure load examples/dev-freeswitch.yaml --pbx-profile dev-ssh --schedule "6:25,12:25" --duration 60 --ramp 2
```

| 指標 | 値 |
|---|---|
| 発信 / 確立 / 失敗 | 12 / 12 / 0 |
| INVITE→200 p50 / p95 | 92.5 / 108.7 ms |
| PBX の channels（N=6 / N=12） | 12 / 24（= 通話数 × 2） |
| freeswitch nlwp（待機 / N=12） | 18 / 42 |
| ESL イベント | {'CHANNEL_CREATE': 60, 'CHANNEL_ANSWER': 24, 'CHANNEL_HANGUP_COMPLETE': 60} |
| 切断理由 | {'LOSE_RACE': 32, 'USER_BUSY': 4, 'NORMAL_CLEARING': 24}（LOSE_RACE は同時鳴動の敗者レグ、USER_BUSY は内線上限に達した内線の 486） |
| ログ行の内訳 | {'WARNING': 20, 'NOTICE': 320, 'INFO': 96} |
| 監視サンプル数 | 28（2 秒周期） |

同一ホスト（dev-local）で N=10 のとき、区間 %CPU は約 8%（ps の平均値は 1.7%）で、リング直後のピークは 19% だった。

## PBX プロファイル

`~/.semishigure/pbx_profiles.yaml`（`SEMISHIGURE_HOME` で変更）。`examples/pbx_profiles.example.yaml` を `semishigure pbx init` で複製できる。
`semishigure pbx test <name>` で接続確認（バージョン、上限値、チャネル数、プロセス、ログ末尾）。画面の Test ボタンも同じ。

シナリオの `pbx_profile:` にプロファイル名を書くと、SIP の接続先（host / port / domain / 環境タグ）はプロファイルが優先され、実行中に PBX ホストを監視する。

## 未対応（次の段階）

- Flatline 固有の status 解析、ログ解析、conf の一時変更は段階 4 のプラグインで実装する（`Monitor.plugins` に `collect_metrics` / `parse_log_line` のフックを用意済み）
- Asterisk アダプタは段階 5
