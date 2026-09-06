# 開発者向けメモ

利用者が向き合うのは実際の PBX です。ここにあるのは、アプリの開発と回帰確認のために PBX を手元に立てる方法と、検証の記録です。リリース物（wheel）には `semishigure` パッケージだけが入り、`deploy/` `docs/` `examples/` `tests/` は含まれません。

## 検証用の PBX

| PBX | 起動方法 | 状態 |
|---|---|---|
| FreeSWITCH 1.10（ソースビルド） | `deploy/freeswitch/run-native.sh`（conf は `deploy/freeswitch/conf`、内線 9100 / 9001〜9004、着信グループ 8001、ESL ClueCon、TLS 5061） | ネイティブ起動で検証済み |
| Asterisk 20（apt） | `deploy/asterisk/render-conf.sh` → `run-native.sh`（127.0.0.1:5090、AMI 5038） | ネイティブ起動で検証済み |
| FusionPBX 5.5 | `deploy/fusionpbx/install-native.sh` → `run-native.sh`（PostgreSQL、PHP 内蔵サーバ 8090） | ネイティブ起動で検証済み |
| Docker（`deploy/freeswitch` と `deploy/asterisk` の Dockerfile / compose） | `docker compose up` | 開発用に残しているが、開発環境でイメージを取得できず未検証 |

いずれも開発時の確認用で、利用者には不要です。同じマシンで複数の PBX を動かすときはポート（SIP 5060 / 5090、ESL 8021 / 8022、RTP 範囲）を分けます。FusionPBX は `external` プロファイルが 5080 を使うので、アプリの `answerer_port` を 5082 などにします。

## 実測の記録

- 段階ごとのレポート: `docs/stage1-report.md` 〜 `docs/stage5-*.md`、`docs/capacity.md`（50 通話）、`docs/fusionpbx-report.md`
- 画面の改修: `docs/ui-ux-proposal.md`（改修案と実施状況）、`docs/ui-ux/`（スクリーンショット）
- 設計上の判断（共有事項 5 節の未決事項を含む）: `docs/decisions.md`
- プラグイン: `docs/plugins.md`

## テストと検査

```bash
pip install -e ".[dev]" httpx
ruff check semishigure tests
pytest -q                                   # ループバック（PBX 不要）: SIP / RTP / コントローラ / API / プロビジョニングの生成と復元
cd tests/ui && npm install && npx playwright install --with-deps chromium
semishigure serve --port 8080 --scenarios ../../examples --no-store &
node check_ui.mjs http://127.0.0.1:8080     # 375 / 768 / 1440 px で横スクロールなし、ページエラーなし、axe-core の serious 以上 0 件
```

CI（`.github/workflows/ci.yml`）は `test`（ruff + pytest）と `ui`（Playwright + axe-core）の 2 ジョブです。PBX を使うテストは含めていません。

## 実機で確認したこと

| 項目 | FreeSWITCH | Asterisk | FusionPBX |
|---|---|---|---|
| REGISTER / INVITE / 同時鳴動 / BYE / CANCEL | ✓ | ✓ | ✓ |
| UDP / TCP / TLS | ✓ | ✓ | UDP |
| DTMF（RFC 2833）/ REFER | ✓ | – | – |
| 負荷制御（5 → 20 → 8、自動減少、50 通話） | ✓ | ✓（20） | ✓（20） |
| 監視（ESL / AMI、プロセス、ログ、イベント） | ✓ | ✓ | ✓ |
| プロビジョニング（作成 → 通話 → 上限 → 削除 → 復元） | ✓ | ✓ | ✓ |
| セットアップガイドの通し実行 | – | – | ✓ |

確認できていないこと: Flatline 一式（プラグインは設定例のみ）、Docker イメージ、Windows（WSL2）上での動作、50 通話を超える規模、正規の CA 証明書での TLS、別ホストへの SSH（検証はコンテナ内の sshd）。

## リリース物の確認

```bash
pip wheel --no-deps -w dist .
unzip -l dist/semishigure-*.whl          # semishigure/ 以下（ui/static のベンダー同梱物と scenario/template.yaml を含む）だけが入る
```

## Windows デスクトップ版のビルド

インストーラは pynsist（MIT）と NSIS（zlib）で作ります。埋め込み Python 3.12 と、依存の Windows 用 wheel、Semishigure の wheel を同梱し、`semishigure.desktop:main`（アプリ窓）を起動する `Semishigure.exe`、コンソール版、CLI の `semishigure.exe` を作ります。

```bash
pip install pynsist pillow          # NSIS: Windows は https://nsis.sourceforge.io/、Linux は apt install nsis
python tools/set_icon.py path/to/icon.png   # アイコンの差し替え（assets/semishigure.ico と画面の favicon を生成）
python tools/build_windows.py       # build/nsis/Semishigure-<版>-setup.exe
```

Linux からのクロスビルドもできます（python.org から埋め込み Python を取得）。GitHub Actions の `release-windows` ワークフローは、`v*` のタグを push すると Windows ランナーで pytest → ビルド → Release への添付まで行います。手動実行（workflow_dispatch）ではアーティファクトとして取得できます。

リリース手順:

1. アイコンを `python tools/set_icon.py <png>` で入れる
2. `pyproject.toml`、`semishigure/__init__.py`、`installer.cfg` の版と `CHANGELOG.md` を更新
3. `git tag v1.0.0 && git push origin v1.0.0`
4. Actions の `release-windows` が Release に `Semishigure-1.0.0-setup.exe` を添付する
