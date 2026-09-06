# Asterisk 対応レポート（設計 段階 5 の前倒し）

作成日: 2026-09-06
背景: 段階 4（Flatline プラグイン）は検証環境を用意できないため保留し、「PBX（Asterisk / FreeSWITCH）と接続し、発信・応答して負荷検証できる」ことを優先した。

環境: 同じコンテナに Ubuntu 24.04 の Asterisk 20.6（chan_pjsip）を apt で入れ、`deploy/asterisk` の設定で 127.0.0.1:5090 に起動（FreeSWITCH は 5060 のまま同居）。AMI は 127.0.0.1:5038。
アプリは同一ホスト実行と、コンテナ内 sshd 経由（AMI はポートフォワード）の両方で確認した。

## 確認したこと

| 項目 | 結果 |
|---|---|
| REGISTER（9001〜9004、ダイジェスト認証） | 200、expires 299 秒 |
| INVITE（9100 → 8001、401 → 認証つき再送） | 100 が 1.3 ms、180 が 12 ms、200 が 62 ms |
| 着信グループ（`Dial(PJSIP/9001&9002&9003&9004)`） | 稼働数最少の内線が応答し、他は CANCEL（Asterisk 側の切断理由 `Answered elsewhere`） |
| 対応付けヘッダー | ダイアルプランの pre-dial ハンドラで `X-Semishigure-Call` と `X-LANG` を B レグにコピー。応答側がグループ化に利用 |
| RTP | 両方向 400 パケット / 8 秒、欠落 0、受信音声の RMS 3700〜4700 |
| 通話制限 20（`GROUP_COUNT`） | 目標 23 で発信すると 21 本目以降が 486。コントローラが 3 回連続で検知し目標を 20 に下げた |
| PBX 監視（AMI） | `core show channels count` / `core show settings` / `pjsip show contacts` / プロセスとスレッド / ログ / Newchannel・Newstate・Hangup イベント |

## 負荷検証（5 → 20 → 8、通話長 60 秒、発信 1 本/秒）

```
semishigure load examples/dev-asterisk.yaml --pbx-profile ast-local --schedule "5:40,20:40,8:40" --duration 60 --ramp 1
```

| 指標 | 値 |
|---|---|
| 発信 / 確立 / 失敗 | 28 / 28 / 0 |
| INVITE→180 p50 / p95 | 10.4 / 11.8 ms |
| INVITE→200 p50 / p95 / max | 62.0 / 63.7 / 64.0 ms |
| 応答内線の分布 | 9001〜9004 に 7 本ずつ |
| RTP 送出遅れ最大 / 5 ms 超 | 4.39 ms / 0 件（tx 61,419 / rx 61,419、欠落 0） |
| Asterisk の channels（N=20） | 40（= 通話数 × 2） |
| Asterisk の %CPU（区間値、N=20） | 6〜8%。リング直後のピーク 15.5% |
| Asterisk の nlwp（待機 / N=20） | 57 / 98 |
| AMI イベント | Newchannel 140、Up 56、Hangup 140 |
| 切断理由 | Normal Clearing 56、Answered elsewhere 63、User busy 21 |

FreeSWITCH（段階 2）との比較: INVITE→200 は FreeSWITCH 約 92 ms、Asterisk 約 62 ms。どちらも 50 ms は応答側の同時鳴動の窓で、残りが PBX の処理時間。

## 実装

- `semishigure/pbx/ami.py`: AMI クライアント（Login、Command、イベント）。自前実装で依存なし
- `semishigure/pbx/adapter.py` の `AsteriskAdapter`: `asterisk -rx` と AMI。プロファイルの `esl_*` を AMI に流用し、`extra.ami_user` / `extra.asterisk_conf` を追加
- AMI の Newchannel / Newstate(Up) / Hangup を FreeSWITCH と同じイベント名に変換し、`ChanVariable(SEMI_CALL)` で自通話に対応付け（`manager.conf` の `channelvars`）
- ログのレベル判定を Asterisk 形式（`WARNING[pid]`）にも対応
- `deploy/asterisk`: pjsip.conf / extensions.conf / manager.conf / rtp.conf のテンプレートと展開スクリプト、Dockerfile、compose、README
- `examples/dev-asterisk.yaml`、`examples/pbx_profiles.example.yaml` の `asterisk-local`

## 制限

- Asterisk の順次鳴動は Dial の書き換えが必要（同時鳴動のみテンプレート化）
- Asterisk のスレッド名は `asterisk` / `SIP` / `timer` 程度で、FreeSWITCH のようなモジュール別の名前は出ない
