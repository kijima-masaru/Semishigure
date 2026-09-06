# Semishigure 検証用 FreeSWITCH（段階 1〜3）

設計書 9.1 の要求を満たす素の FreeSWITCH 1.10.12 です。SignalWire のパッケージトークンを使わず、
ソースから最小モジュール構成でビルドします（Debian bookworm、ビルド 5〜15 分）。

| 設計 9.1 の要求 | この環境の値 |
|---|---|
| SIP ドメイン | `pbx.semishigure.test`（`SEMI_FS_DOMAIN`） |
| internal プロファイル | UDP 5060（`SEMI_FS_SIP_PORT`） |
| RTP ポート範囲 | 16384〜32768（`SEMI_FS_RTP_START` / `SEMI_FS_RTP_END`） |
| 発信用内線 | 9100（PBX 側の同時数制限なし） |
| 応答用内線 | 9001〜9004（PBX 側の制限なし。アプリの `max_calls` で 5 本ずつに制限） |
| 着信グループ | 入口番号 8001、宛先 9001〜9004、同時鳴動（`SEMI_FS_RING_SEP=,`。`|` で順次） |
| 着信グループの通話制限 | 20（`SEMI_FS_RING_GROUP_LIMIT`。超過は 486 Busy Here） |
| max-sessions / sessions-per-second | 100 / 30 |
| 内線パスワード | `SEMI_FS_EXT_PASSWORD`（既定 `semishigure-dev`。開発専用） |
| ESL | 127.0.0.1:8021、パスワード `SEMI_FS_ESL_PASSWORD`（既定 `ClueCon`） |
| テスト用番号 | 9196 エコー、9197 1kHz トーン、`9xxx` 内線直通 |

`X-LANG-DETECT=true` は FusionPBX の手順（STEP3-5）と同じく着信グループのダイアルプランで set しています。

## 起動方法

### A. WSL2（Ubuntu）の Docker で動かす（推奨）

アプリも WSL2 内で動かします。ホストネットワークなので RTP ポート範囲の公開が要りません。

```bash
cd deploy/freeswitch
docker compose up -d --build
docker compose logs -f            # "FreeSWITCH ... is ready" が出れば起動
docker exec semishigure-fs fs_cli -x "sofia status"
```

PBX の IP は WSL2 の eth0 のアドレス（`hostname -I`）です。`SEMI_FS_SIP_IP` を省略すると
FreeSWITCH がそのアドレスに bind します。アプリ側は `examples/dev-freeswitch.yaml` の `pbx.host` にこの IP を書きます。

### B. Docker Desktop（Windows）で動かす

Docker Desktop 4.34 以降は Settings → Resources → Network → "Enable host networking" で
`network_mode: host` が使えます。使えない場合はポート公開方式にします:

```yaml
    # network_mode: host をやめて
    ports:
      - "5060:5060/udp"
      - "16384-16584:16384-16584/udp"   # 範囲を狭める（同時 20 通話なら 200 ポートで十分）
    environment:
      SEMI_FS_SIP_IP: "0.0.0.0"
      SEMI_FS_EXT_SIP_IP: "<Windows ホストの IP>"
      SEMI_FS_EXT_RTP_IP: "<Windows ホストの IP>"
      SEMI_FS_RTP_START: "16384"
      SEMI_FS_RTP_END: "16584"
```

### C. Linux ホストにネイティブで動かす

`Dockerfile` と同じ手順で `/usr/local/freeswitch` にビルドした後:

```bash
SEMI_FS_SIP_IP=127.0.0.1 deploy/freeswitch/run-native.sh
```

## 確認コマンド

```bash
fs_cli -x "sofia status profile internal reg"   # 9001〜9004 の登録
fs_cli -x "show channels count"
fs_cli -x "status"                              # max sessions / per Sec
tail -f log/freeswitch.log
```

## 段階 3 用の SSH

`SEMI_FS_SSH=1` と `SEMI_FS_SSH_PUBKEY` を設定すると、コンテナ内で sshd（ポート 2222、ユーザー `semi`、鍵認証のみ）が起動し、
`fs_cli` / `ps -L` / `/var/log/freeswitch/freeswitch.log` が参照できます。

## 社内プロキシの CA

ビルド中の git / apt がプロキシ CA を必要とする場合は、PEM 形式の証明書を `ca/*.crt` として置いてください（git には含めません）。

## TLS

起動時に自己署名証明書を生成し、5061（`SEMI_FS_TLS_PORT` / `SEMI_AST_TLS_PORT`）で SIP over TLS を待ち受けます。証明書の検証はしません（検証用）。
Semishigure 側はプロファイルの `sip_transport: tls` と `tls_verify: false`、または シナリオの `pbx.transport: tls` で接続します。TCP は UDP と同じポートです。
