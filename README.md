# Semishigure（蝉時雨）

FreeSWITCH / Asterisk に SIP で接続し、同時通話の負荷テストを行う Python アプリです。
SIPp（発信）と pjsua（応答）の役割を自前の SIP / RTP 実装で担い、外部ツールを使いません。

- 発信 UAC: INVITE（ダイジェスト認証、任意ヘッダー）→ 200 → ACK → RTP → BYE / CANCEL
- 応答 UAS: 内線ごとの REGISTER（更新・再登録）、自動応答、内線ごとの同時通話上限（486）
- メディア: WAV（8kHz/16bit/mono）を G.711 に変換し、20ms ごとに送出。送出遅れを通話ごとに計測
- 秘密情報は YAML に書かず `secret:NAME` 参照（環境変数または暗号化ストア）
- 異常終了時（SIGINT / SIGTERM / 例外）に全通話の BYE と REGISTER 解除を実行

設計書: `Semishigure 設計.md`（別管理）。段階計画は設計書 7 章。**現在は段階 1（SIP コア）完了**。

## 構成

```
semishigure/
  sip/        SIP 信号: message / sdp / auth / transport / transaction / dialog / endpoint / uac / uas
  media/      メディア境界 (base) と Python 実装 (engine): codec / wav / rtp / pump
  core/       call（通話記録・指標）, engine（配線と安全停止）
  scenario/   シナリオ YAML モデル
  plugins/    サービスプラグイン（flatline は段階 4）
  secrets.py  秘密情報ストア
  cli.py      コマンドライン
deploy/freeswitch/   検証用 FreeSWITCH（Dockerfile / compose / conf）
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

## 検証環境（FreeSWITCH）

`deploy/freeswitch/README.md` を参照（Docker compose、WSL2 推奨）。

## 段階 1 の確認コマンド

```bash
semishigure audio synth audio/speech_en.wav --seconds 60 --pitch 160 --seed 1
semishigure audio synth audio/speech_ja.wav --seconds 60 --pitch 230 --seed 2
export SEMISHIGURE_SECRET_EXT=<内線パスワード>     # または: semishigure secret set ext
semishigure -v call examples/dev-freeswitch.yaml --duration 20 --record-rx runs/rx --report runs/report.json
```

出力例と実測は `docs/stage1-report.md`。

## 制約（共有事項 6 節）

- SIPp / pjsua / PJSIP / ESL ライブラリは使わない。依存は MIT / Apache-2.0 / BSD / EPL-2.0 / LGPL のみ
  - 実行時: pyyaml (MIT), cryptography (Apache-2.0 / BSD)。段階 2 以降: fastapi (MIT), uvicorn (BSD), websockets (BSD), asyncssh (EPL-2.0), openpyxl (MIT)
- 内線パスワードや SSH 鍵パスフレーズは YAML やコードに書かない
- `prod` 環境タグでは同時数上限の強制と実行前確認（段階 2 で実装）
