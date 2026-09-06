# 判断記録（共有事項 5 節の未決事項と、実装中に決めたこと）

作成日: 2026-09-06（段階 1）

## 共有事項 5 節の未決事項

| # | 未決事項 | 判断 | 理由 |
|---|---|---|---|
| 1 | 初版の動作確認を Windows から SSH で行うか、PBX ホスト上でアプリを動かすか | **PBX と同じホスト（WSL2 / Linux）でアプリを動かす**。Windows から使う場合も WSL2 内で `semishigure` を実行する | (a) FreeSWITCH を Docker で立てると RTP ポート範囲（16384〜32768）を Windows 側へ公開するのが重く、NAT で SDP の IP が食い違いやすい。(b) RTP 送出の 20ms 精度は Linux の方が安定する（Windows はタイマー分解能 1ms）。(c) 段階 3 の `LocalExecutor` で fs_cli / ps / ログを直接読める。SSH 接続（`SshExecutor`）は段階 3 でコンテナ内 sshd（`SEMI_FS_SSH=1`）に対して検証する |
| 2 | 応答用内線を `limit_max=5` × 4 本の前提で維持するか | **維持する**。アプリの `max_calls: 5` が pjsua の `--max-calls=5` に相当し、超過時は 486 を返す。素の FreeSWITCH 側には内線ごとの制限を入れず、着信グループの通話制限 20 だけを `limit hash` で掛ける | FusionPBX の実環境（limit_max=5）と同じ挙動をアプリ側で再現できる。PBX 側の制限は環境依存なので、設定を増やさず着信グループの制限だけ合わせた |
| 3 | 記録表を既存 xlsx の「記録表」シートと同じ列構成にするか | **同じ列構成にする**（段階 4 で実装）。汎用の行（channels、CPU、SIP 統計、RTP 遅れ）は本体が出し、Flatline 固有の行（ws conns、workers、net loop、検出ロック、TTS 等）はプラグインが追加する | 既存の記録と比較できることが目的。行の定義をプラグインに委ねれば本体は汎用のまま |
| 4 | Asterisk 対応を段階 5 のままとするか | **段階 5 のまま** | 段階 1〜4 の目的は Flatline（FreeSWITCH）の測定。SIP エンジンは PBX 非依存に作ってあり（段階 1 は PBX 固有処理を含まない）、段階 5 で必要なのはアダプタ（`asterisk -rx` / AMI）だけ |

## 実装中に決めたこと

| 項目 | 判断 | 理由 |
|---|---|---|
| 検証環境の作り方 | FreeSWITCH 1.10.12 をソースから最小モジュールでビルド（`deploy/freeswitch/Dockerfile`） | SignalWire の apt リポジトリはトークン登録が必要で、Docker Hub の公式イメージは非公開になった。ソースビルドなら依存が Debian 標準パッケージだけで済む |
| 着信グループの呼び出し方式 | 同時鳴動（`,`）を既定にし、`SEMI_FS_RING_SEP=|` で順次に切替 | FusionPBX の ring group 既定（simultaneous）に合わせた |
| 同時鳴動時の応答の割り当て | 同一発信者からの INVITE を 50ms の窓でまとめ、稼働通話数が最少の内線だけが応答する。他の内線は PBX からの CANCEL を待ち、1 秒来なければ応答する | pjsua 方式（全内線が即応答）だと PBX が勝者以外に BYE を送り、0.02 秒の「幽霊通話」が統計に混ざる。順次鳴動や内線 1 本の環境でも同じコードで動く |
| 秘密情報の保管 | `secret:NAME` 参照。解決順は環境変数 `SEMISHIGURE_SECRET_<NAME>` → 暗号化ファイル `~/.semishigure/secrets.enc`（cryptography の Fernet、鍵は `SEMISHIGURE_MASTER_KEY` または `~/.semishigure/master.key`） | OS キーチェーン連携（keyring）は Windows / macOS / Linux で挙動が異なるため初版は暗号化ファイルにした。YAML に平文を書くと `SecretError` で拒否する |
| G.711 変換 | 自前テーブル（16384 エントリ）で実装し、CPython の `audioop` と全 65536 値で一致を確認 | 設計 3.2 のとおり（`audioop` は 3.13 で削除）。WAV は通話開始前に丸ごと符号化し、ポンプはバイト列を切り出すだけにした |
| SIP トランザクション | RFC 3261 §17 に加え RFC 6026 の accepted 状態を実装（2xx の再送を UAS が ACK まで行い、UAC は再送 2xx に ACK を返す） | UDP で ACK が落ちたときに通話が宙に浮くのを防ぐ |
| 音源ファイル | 実音声が無い環境用に `semishigure audio synth` で「音声らしい」合成波形を生成 | 段階 1 の「WAV が RTP で流れる」確認には無音でないことが分かれば足りる。段階 4 では実際の英語・日本語 WAV を使う |
