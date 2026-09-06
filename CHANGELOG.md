# Changelog

## 1.0.0 (2026-09-06)

最初のリリース。

- SIP / RTP スタック内蔵の同時通話負荷検証（発信側と応答側、UDP / TCP / TLS、G.711、DTMF、REFER）
- 目標同時数のリアルタイム制御（段階実行、プリセット、バースト、自動減少、prod の確認と上限）
- PBX 接続と監視（FreeSWITCH の ESL、Asterisk の AMI、SSH 経由、プロセスとログ、イベントと切断理由）
- プラグイン機構（log_patterns / status_command / conf_override / ws_hook / webhook）
- 記録（SQLite、記録表 xlsx、画面での詳細と比較）
- 画面（ガイド / 実行 / シナリオ / PBX / 結果、ライト / ダーク、アクセシビリティ検査）
- セットアップガイド（PBX 接続 → 内線を作る → 内線と番号 → シナリオ → 事前チェック → 負荷検証）
- PBX 側の内線と着信グループの作成と削除（FreeSWITCH / Asterisk / FusionPBX）
- Windows デスクトップ版（インストーラ、アプリ窓）
