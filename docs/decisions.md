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

## 段階 2 で決めたこと

| 項目 | 判断 | 理由 |
|---|---|---|
| 同時鳴動の通話の対応付け | 発信側の INVITE に `X-Semishigure-Call: <通話ID>` を付け、応答側はこの値で兄弟 INVITE をまとめる（`caller.correlation_header`。空文字で無効化し、発信者番号と 50 ms の時間窓に戻る） | FreeSWITCH は既定（`sip_copy_custom_headers`）で X- ヘッダーを B レグにコピーする。同一発信者の別通話が同時に鳴ると時間窓だけでは混ざり、敗者レグが 1 秒後に応答して INVITE→200 が 1 秒に見える不具合があった |
| PBX の解放待ち | 終了直後の通話（失敗を含む）を `teardown_grace`（0.5 秒）の間は占有中と数えて補充を遅らせる。drain の判定には使わない | 着信グループの `limit` は A レグ破棄で解放されるため、BYE 直後に補充すると 486 になる。失敗直後の再発信も同じ理由で間を空ける |
| 486/503 連続時の自動減少 | 3 回連続で目標 N を「確立数」まで下げ、理由を UI に表示する | 設計 3.4 の安全装置。限界の自動探索にも使える |
| prod の扱い | 環境タグ `prod` は `confirm_prod` なしでは開始できず、同時数上限を 20 に固定する。絶対上限は 50 | 共有事項 6 節の制約 |
| Vue の読み込み | CDN（jsdelivr）を第一にし、読めなければ同梱の `vue.global.prod.js`（MIT）に切り替える | PBX ホストや閉域網では CDN に届かない。同梱ファイルは npm の vue@3.4.38 を無改変で置いたもの |
| ランの保存 | SQLite（`~/.semishigure/runs.sqlite3`）に runs / samples（1 秒） / calls / events を 5 秒ごとに追記 | 設計 3.6。異常終了しても途中までの時系列が残る |

## 段階 3 で決めたこと

| 項目 | 判断 | 理由 |
|---|---|---|
| API コマンドの経路 | ESL（自前の TCP クライアント）を第一にし、SSH 時はポートフォワードで 127.0.0.1:8021 に接続する。ESL に接続できないときだけ `fs_cli -x` に落とす | 設計 5 章の方針。fs_cli はパスワードがコマンドラインに出るうえ、パスや権限が環境ごとに違う |
| CPU の取り方 | `ps -o pcpu` は起動からの平均なので、`/proc/<pid>/stat` と `task/*/stat` の jiffies 差分で 2 秒区間の %CPU を出す。ps の値も `cpu_avg` として残す | 手順書の monitor.sh は ps を使うが、net thread の飽和を見るには区間値が要る |
| SSH のホスト鍵 | 既定は known_hosts で厳格に検証（`ssh_known_hosts` 未指定なら `~/.ssh/known_hosts`）。`ssh_strict_host_key: false` は開発専用 | 鍵認証のみという共有事項の方針に合わせ、なりすましも防ぐ |
| SSH 鍵のパスフレーズ / ESL パスワード | プロファイルには `secret:NAME` 参照だけを書く。API で値を直接書こうとすると 400 で拒否 | 共有事項 6 節 |
| 通話イベントの対応付け | ESL の CHANNEL_* イベントで `variable_sip_h_X-Semishigure-Call` を見て自通話に対応付け、切断理由と billsec を記録 | 段階 2 で入れたヘッダーをそのまま使える。PBX 側の視点（USER_BUSY / LOSE_RACE 等）が取れる |
| プラグインのフック | `Monitor.plugins` の `collect_metrics(adapter)` と `parse_log_line(line)` を本体側に用意し、Flatline 固有の解析は段階 4 で `plugins/flatline` に閉じ込める | 本体を汎用に保つ |

## Asterisk 対応（段階 5 の前倒し）で決めたこと

| 項目 | 判断 | 理由 |
|---|---|---|
| 段階の順序 | 段階 4（Flatline）を保留し、段階 5 の Asterisk 対応を先に実施 | 段階 4 の検証環境（Flatline 一式、Deepgram、ChatAPI、OpenAI、実音声）を用意できない。「PBX と接続して発信・応答・負荷検証ができる」が優先事項 |
| Asterisk の入手 | Ubuntu 24.04 の apt パッケージ（20.6、chan_pjsip）。Docker も `ubuntu:24.04` + apt | ソースビルドより速く、pjproject の同梱ダウンロード（GitHub のアーカイブ）がプロキシで遮断される環境でも動く |
| API コマンドの経路 | AMI の `Command` アクション（自前 TCP クライアント）を第一にし、`asterisk -rx` にフォールバック。設定は FreeSWITCH の ESL と同じ項目（`esl_host` / `esl_port` / `esl_password_ref`）を流用し、ユーザー名は `extra.ami_user` | プロファイルの項目を増やさず両 PBX を同じ形で扱う |
| 通話イベントの対応付け | ダイアルプランで `Set(__SEMI_CALL=${PJSIP_HEADER(read,X-Semishigure-Call)})` し、`manager.conf` の `channelvars=SEMI_CALL` でイベントに載せる | Asterisk のイベントには SIP ヘッダーが載らないため |
| B レグへのヘッダー転送 | Dial の pre-dial ハンドラ（`b(default^predial^1)`）で `X-Semishigure-Call` と `X-LANG` を追加 | FreeSWITCH は自動でコピーするが Asterisk はしない。応答側のグループ化は時間窓へのフォールバックでも動くが、ヘッダーがあれば確実 |
| 着信グループの通話制限 | `GROUP()` / `GROUP_COUNT()` で 20 を超えたら `Busy()`（486） | FusionPBX の limit と同じ挙動。負荷側の自動減少が Asterisk でも働くことを確認 |
| Asterisk の GPL | PBX 側のソフトとして使うだけで、アプリには組み込まない。`deploy/asterisk` は設定ファイルと Dockerfile のみ | 依存ライブラリの制約（GPL 不可）はアプリ本体に対するもの |
