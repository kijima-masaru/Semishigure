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

## 段階 4（プラグイン機構）で決めたこと

| 項目 | 判断 | 理由 |
|---|---|---|
| 段階 4 の位置づけ | Flatline 専用プラグインではなく、汎用のプラグイン機構と同梱プラグイン 5 種（`log_patterns` / `status_command` / `conf_override` / `ws_hook` / `webhook`）を実装。Flatline のケースは `examples/plugins-example.yaml` の設定例として示す | 検証環境がなく Flatline 固有コードは確認できない。汎用部品にしておけば他の PBX やサービスにも使え、Flatline は設定だけで組める |
| フックの形 | `pre_run` / `post_run` / `on_call_established` / `on_call_ended` / `collect_metrics` / `parse_log_line` / `snapshot` / `report_rows`。すべて省略可能で、1 プラグインの例外はそのプラグインの `errors` に閉じ込める | 設計 3.5 のフック一覧に沿い、プラグインの不具合で負荷テストが止まらないようにする |
| post_run の保証 | `SipEngine.shutdown_hooks` に登録し、通話の BYE と同じ経路（SIGINT / SIGTERM / 例外 / 通常停止）で必ず実行。二重実行は抑止 | 共有事項 6 節「異常終了時に conf 一時変更の復元」 |
| conf_override の書き方 | XML の `<param name value>` はパラメータ名指定、それ以外は正規表現置換。バックアップは `<path>.semishigure.bak`、復元は `mv` で原子的に | FreeSWITCH の conf と Asterisk の ini 形式の両方に対応。復元が中断してもバックアップが残る |
| ws_hook のプレースホルダ | `{var.NAME}`（uuid_getvar で取るチャネル変数）、`{header.NAME}`（発信 INVITE のヘッダー）、`{pbx_uuid}`、`{secret:NAME}` | loadtest_operator.py の「chat_uuid を取って接続し、9001 を待って openChat を送る」を設定だけで表せる。PBX 側の uuid は ESL / AMI の対応付けから得る |
| 外部プラグイン | `module: パッケージ.モジュール:クラス` で任意のクラスを読み込む | エントリポイント登録より単純で、シナリオ YAML だけで完結する |

## 通話ステップと記録表で決めたこと

| 項目 | 判断 | 理由 |
|---|---|---|
| ステップと通話長 | `steps` があればステップが通話を進め、`call_duration` は上限（超えたら BYE）として残す | UI からの通話長変更と、ステップの途中で相手が切る場合の両方に対応 |
| DTMF 送出中の音声 | telephone-event の間は音声フレームを送らない | 電話機と同じ振る舞い。PBX 側で音声と DTMF が混ざらない |
| REFER の完了判定 | 202 を受けたあと NOTIFY（sipfrag）に 200 を返し、PBX からの BYE で終了を判定 | FreeSWITCH は転送成立後に発信側へ BYE を送る。NOTIFY の内容に依存しない |
| 記録表の列構成 | 行 = 指標、列 = ラン。汎用の行は本体、サービス固有の行はプラグインの `report_rows` | 手順書の記録表と同じ向き。ラン同士の比較がしやすい |
| 「安定時」の値 | ランの後半 50% のサンプルの平均と、全体の最大 | 立ち上げ中の値を除く単純なルール。必要なら後で窓を指定できるようにする |

## TLS / TCP トランスポートで決めたこと

| 項目 | 判断 | 理由 |
|---|---|---|
| ストリームの扱い | 待ち受け 1 つ（Contact のポート）と、宛先ごとのオンデマンド接続。応答は要求が届いた接続に返す | PBX は登録先の Contact（`;transport=tls`）へ新しい接続を張ってくるので、待ち受けが要る。発信側は同じ接続で応答を受ける |
| 証明書 | 応答側の待ち受け用に自己署名証明書を `~/.semishigure/tls/` に自動生成（EC P-256、SAN にローカル IP）。PBX 側の検証は既定で有効、検証用 PBX だけ `tls_verify: false` | 検証用 PBX は自己署名で、`tls-verify-policy none` にしている。本番向けには CA 指定で検証できる形を残す |
| SRTP | 未対応。RTP は UDP のまま | 設計の対象外。メディア境界の差し替えで将来対応できる |
| Asterisk の TCP | UDP と同じポート番号に TCP トランスポートを追加 | FreeSWITCH の sofia は既定で UDP と TCP を同じポートで待つため、両 PBX で同じ設定になる |

## 画面の仕上げで決めたこと

| 項目 | 判断 | 理由 |
|---|---|---|
| シナリオ編集 | GUI フォームではなく YAML エディタ + 保存時の検証 + 解釈結果の表示 | ヘッダー、ステップ、プラグイン設定は自由度が高く、フォームにすると YAML でしか書けない項目が残る。検証と解釈結果で誤りは防げる |
| PBX プロファイル編集 | サーバの dataclass の項目から自動生成したフォーム | 項目を増やしたときに画面の変更が要らない |
| 事前チェック | コントローラを止めた状態で 1 本だけ発信し、両端の RTP の音量（RMS）まで見る | 無音でも「通った」ように見える不具合を防ぐ。手順書の STEP7〜9 相当 |
| CI | GitHub Actions で ruff と pytest。PBX を使うテストは含めない | ループバックテストだけで SIP / RTP / コントローラ / API を検証できる |

## RTP 送出部の別言語化について

| 項目 | 判断 | 理由 |
|---|---|---|
| Go / Rust への差し替え | 行わない（境界だけ維持） | 50 通話（メディア 100 セッション）で 5 ms 以内 99.7%、欠落 0、失敗 0。設計の目標を Python 実装で満たしている（`docs/capacity.md`） |

## FusionPBX 経由の FreeSWITCH で決めたこと

| 項目 | 判断 | 理由 |
|---|---|---|
| 導入方法 | FusionPBX 5.5 をネイティブ導入し、管理画面は PHP の内蔵サーバ（`router.php`）で動かす。FreeSWITCH は `deploy/freeswitch` のソースビルドに `mod_lua` / `mod_pgsql` などを足したもの | この環境では Docker イメージと PPA が取れない。Semishigure が接続するのは FreeSWITCH なので、管理画面のサーバ構成（nginx + php-fpm か内蔵サーバか）は検証結果に影響しない |
| 内線パスワード | FusionPBX が自動生成した値をそのまま使い、`secret:fusion_9100` などの参照で渡す | 共有事項 6 節（秘密情報を YAML に書かない）。画面で決めたパスワードを YAML に写す手順を残さない |
| 応答側の待ち受けポート | `answerer_port: 5082` | FusionPBX の `external` プロファイルが 5080 を bind する |
| 着信グループの Destination | 作成後に Enabled が True であることを確認する手順を README に入れる | 5.5 の画面で追加した宛先が無効のまま保存され、鳴らないことがあった |
| `limit_max` の扱い | アプリの `max_calls` を `limit_max` と同じ 5 にし、PBX 側の制限は直接発信の検証でだけ確認 | 着信グループ経由では FusionPBX の `limit_max` が掛からないため、アプリ側で同じ挙動を作る（決定 2 と同じ） |
| 応答時間の差 | INVITE→200 が素の FreeSWITCH より約 90 ms 長いのは PBX 側（Lua + PostgreSQL）の処理として記録し、アプリ側では補正しない | 手順書の目的は PBX の処理時間を N ごとに見ることで、差そのものが記録対象 |

## 画面の改修（docs/ui-ux-proposal.md の実施）で決めたこと

| 項目 | 判断 | 理由 |
|---|---|---|
| 既定テーマ | `prefers-color-scheme` に追従し、ヘッダーのボタンで システム / ライト / ダーク を切替（localStorage に保存） | 提案書 7 節。手順書のスクリーンショットはライトのまま使え、スキルの推奨するダークも同じトークンで提供できる |
| フォント | IBM Plex Sans と JetBrains Mono の欧文 woff2（OFL、各 3 ウェイト、計 136 KB）を同梱し、日本語はシステムフォント | 日本語サブセットは 1 ウェイトで 1 MB を超えるため同梱しない。CDN には依存しない |
| チャート | uPlot 1.6.31（MIT、50 KB）を `ui/static/vendor/` に同梱。凡例は uPlot の HTML 凡例（カーソル位置の値）、色は CSS トークンから取得しテーマ切替で作り直す | 手描き Canvas の目盛・二軸・DPR・ツールチップを自作しない。アニメーションが無く reduced-motion に合う |
| 事前チェックの状態 | `state.precheck_run` を `state.run` と分け、スナップショットに `precheck` を載せる | 事前チェック中に画面がラン中 UI に切り替わる問題（提案 F1）の根本原因 |
| 段階実行の開始 | `/api/run/start` に `schedule` / `preset` を追加し、開始直後にスケジュールを適用 | 手順書の「5 → 10 → 20」を開始前に指定できる（提案 F6） |
| 記録表との対応 | `/api/runs/{id}/rows` が xlsx と同じ `generic_rows()` を返し、詳細と比較に表示 | 画面に無い指標を xlsx で探す往復を無くす（提案 C4） |
| 長いランの詳細 | `/api/runs/{id}?max_points=1500` でバケットごとの最大値を残して間引く | ピーク（RTP 遅れ、失敗）を消さずに描画量を抑える（提案 C8） |
| URL | `location.hash`（`#run`, `#results/12`）でタブと詳細を同期 | サーバ側のルーティング変更なし |
| ファイル構成 | `index.html`（テンプレート）/ `styles.css` / `app.js` / `charts.js` に分割。ビルド工程は入れない | 482 行の単一ファイルは保守しにくい。ES modules や bundler は不要 |
| 画面の検査 | `tests/ui/check_ui.mjs`（Playwright + axe-core）で 375 / 768 / 1440 の横スクロール、ページエラー、serious 以上の違反を CI で確認 | 提案 6 節 |

## セットアップガイドで決めたこと

| 項目 | 判断 | 理由 |
|---|---|---|
| 形 | 5 ステップのウィザード（PBX 接続 → 内線と番号 → シナリオ → 事前チェック → 負荷検証）。各ステップは実機に対する確認（接続テスト / 秘密情報の解決 / YAML の検証 / 事前チェック / ラン開始）に合格すると次へ進める | 「設定したつもり」で負荷を掛けるのを防ぐ。手順書の STEP と対応する |
| パスワードの入力 | ガイドから暗号化ストアに保存する API（`POST /api/secrets/{name}`）を追加。応答は名前だけで、値は返さない。YAML には `secret:名前` だけを書く | 共有事項 6 節を守りつつ、CLI を使わずに設定を終えられるようにする。環境変数で渡す方法も残す |
| シナリオの生成 | サーバ側（`POST /api/guide/scenario`）で dict → 検証 → YAML 出力 | クライアントで YAML を組み立てると引用符や型で壊れやすい。既存の検証（`scenario_from_dict`）をそのまま通す |
| 事前チェックの NG | 項目名ごとに確認点を表示。SIP の 4 項目が合格なら監視の NG は無視して進める | 監視の NG は記録に PBX 側の値が残らないだけで、負荷検証自体は可能 |
| 進み具合の保存 | localStorage（パスワードの値は含めない） | ブラウザを閉じても続きから再開できる。サーバに状態を持たせない |
| 初回の誘導 | PBX プロファイルが 0 件で URL にハッシュが無いときだけガイドを開く | 既存の利用者の導線を変えない |

## PBX 側の設定をアプリから作る（プロビジョニング）で決めたこと

| 項目 | 判断 | 理由 |
|---|---|---|
| 作るもの | 発信側 1 内線、応答側 n 内線、着信グループ（同時鳴動 + 上限）、内線への直接発信 | 手順書の構成（9100 → 8001 → 9001〜9004）をそのまま作る。IVR やキューは対象外 |
| 書き方 | FreeSWITCH は XML ファイルの追加、Asterisk は `#include` する別ファイルの追加、FusionPBX は SQL で行を追加 | 既存の設定を編集せず、削除で元に戻せる形にする。FusionPBX は管理画面が作る行をそのまま再現し、ダイアルプランも `app.lua ring_groups` の Lua 呼び出しにする |
| 既存ファイルへの変更 | include 行の追加だけ。変更前に `<path>.semishigure-loadtest.bak` を取り、`unprovision` で `mv` で戻す | conf_override プラグインと同じ方式 |
| パスワード | 英数字 16 文字を生成し、PBX の設定に書き、暗号化ストアに `secret:<接頭辞>_<内線>` で保存。記録ファイル・API 応答・ログには出さない | PBX が知っている必要がある値なので PBX 側には書く。アプリ側は共有事項 6 節を守る |
| FusionPBX の既存内線 | 削除せずそのまま使い、DB にあるパスワードを暗号化ストアへ写す | 管理画面で作った内線を壊さない。削除するのは自分が挿入した行だけ |
| FusionPBX の DB 資格情報 | PBX ホストの `/etc/fusionpbx/config.conf` を読む。読めなければ `secret:` で受け取る | 追加の入力を減らす。`psql` の実行時に `PGPASSWORD` が環境変数として渡る点は README に明記 |
| 反映 | 監視用の ESL / AMI（アダプタ）で `reloadxml` / `module reload res_pjsip.so`、使えなければ CLI（`fs_cli -x` / `asterisk -rx`、`extra.asterisk_conf` があれば `-C`） | 接続テストと同じ経路を使う |
| 検証 | 素の FreeSWITCH（第 2 インスタンス、ESL 8022）、Asterisk 20、FusionPBX 5.5 の 3 系統で作成 → REGISTER → 着信グループ経由の通話 → 上限（486）→ 削除 → 設定の復元を確認 | 単体テストは一時ディレクトリに対する生成と復元 |
