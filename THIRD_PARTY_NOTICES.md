# Third-party notices

Semishigure は次のソフトウェアを利用・同梱しています。いずれも MIT / Apache-2.0 / BSD / EPL-2.0 / OFL / PSF のライセンスで、GPL のものは含みません。

## Python パッケージ（実行時）

| パッケージ | ライセンス | 用途 |
|---|---|---|
| PyYAML | MIT | シナリオとプロファイルの YAML |
| cryptography | Apache-2.0 / BSD-3-Clause | 暗号化ストア（Fernet）、TLS の自己署名証明書 |
| FastAPI, Starlette, Pydantic | MIT | API と WebSocket |
| uvicorn | BSD-3-Clause | HTTP サーバ |
| websockets | BSD-3-Clause | WebSocket |
| asyncssh | EPL-2.0（無改変で利用） | SSH での PBX ホスト接続 |
| openpyxl | MIT | 記録表 xlsx |
| Python 3.12（デスクトップ版に埋め込み） | PSF License | ランタイム |

## 画面に同梱しているもの（`semishigure/ui/static/vendor/`）

| 名前 | ライセンス | 用途 |
|---|---|---|
| Vue 3 | MIT（`LICENSE-vue`） | 画面のフレームワーク |
| uPlot | MIT（`uPlot.LICENSE`） | チャート |
| IBM Plex Sans | SIL Open Font License 1.1（`fonts/IBM-Plex-Sans.LICENSE`） | 本文の欧文 |
| JetBrains Mono | SIL Open Font License 1.1（`fonts/JetBrains-Mono.LICENSE`） | 数値と等幅表示 |

## ビルドとテストにだけ使うもの（配布物には含まない）

pynsist（MIT）、NSIS（zlib）、Pillow（MIT-CMU）、pytest（MIT）、pytest-asyncio（Apache-2.0）、ruff（MIT）、Playwright（Apache-2.0）、axe-core（MPL-2.0、検査ツールとして実行するのみ）。
