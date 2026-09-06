# UI/UX 調査ブリーフ（ui-ux-pro-max スキルの適用結果）

対象: Semishigure（PBX 同時通話 負荷テストツール）の Web 画面。
- 画面: semishigure/ui/static/index.html（Vue 3 CDN、単一ファイル、Canvas 手描きチャート）、API: semishigure/api/app.py（FastAPI + WebSocket /ws）
- 利用者: 通信エンジニア・PBX 運用者。手順書（負荷テスト手順書）に沿って「事前チェック → 同時数 N を段階的に上げる → 記録表に転記」する作業を行う。
- 4 タブ: 実行（Run）/ シナリオ（YAML 編集）/ PBX（プロファイル編集）/ 結果（ラン一覧・詳細・xlsx）
- スクリーンショット: /tmp/claude-0/-home-user-Semishigure/bdc1c521-9e0a-562f-a459-798740ab1d2a/scratchpad/ui_shots/{run,scenarios,profiles,results}_{1440,768,375}.png、running_{1440,375}.png（ラン中の実行画面）
- 実測: 768px と 375px で実行・シナリオ・結果タブに横スクロールが発生（scrollWidth 1338 / 1339 / 1057）。PBX タブも 375px で 425。

## スキル（ui-ux-pro-max）が返した設計方針
- 製品分類: Developer Tool / IDE → 推奨スタイル「Dark Mode (OLED) + Minimalism & Swiss Style」、ダッシュボード様式「Real-Time Monitor + Terminal」、配色「Dark syntax theme + Blue focus」
- パターン: Real-Time / Operations。「telemetry を live と表示するのは現在のソースに裏付けがある時だけ。更新時刻と stale 状態を出す。更新頻度の制御、キーボード操作、reduced motion 時は静的スナップショット」
- 配色案（design system 出力）: Primary #1E293B / Secondary #334155 / Accent #22C55E / Background #0F172A / Foreground #F8FAFC / Card #1B2336 / Muted #272F42 / Muted FG #94A3B8 / Border #475569 / Destructive #EF4444。Status colors green/amber/red。
- タイポグラフィ: JetBrains Mono（見出し・数値）+ IBM Plex Sans（本文）。Mood: code, developer, technical, precise
- 密度: density 8/10（8–32px の spacing scale）
- アンチパターン: Slow updates / No automation / Light mode default（ダーク前提の提案。ただし既存はライト。両対応がスキル上の要件: dark-mode-pairing）

## スキルの Quick Reference で優先すべき規則（優先順）
1. Accessibility (CRITICAL): color-contrast 4.5:1、focus-states（2–4px の可視フォーカス）、aria-labels（アイコンのみボタン）、keyboard-nav、form-labels（label for）、heading-hierarchy（h1→h2 の順序）、color-not-only、reduced-motion、focus-not-obscured、web-target-size 24×24 CSS px、contextual-live-badge-updates（変化する数値は aria-live で文脈つきに）
2. Touch & Interaction (CRITICAL): loading-buttons（非同期中は disable + spinner）、error-feedback（問題の近くに）、cursor-pointer、press-feedback
3. Performance (HIGH): content-jumping（非同期コンテンツの領域確保）、virtualize-lists（50 件以上）、debounce-throttle、progressive-loading
4. Style Selection (HIGH): no-emoji-icons（✔✖ 等の文字アイコン禁止 → SVG）、consistency、primary-action（画面に主 CTA は 1 つ）、state-clarity、elevation-consistent
5. Layout & Responsive (HIGH): horizontal-scroll 禁止、breakpoints 375/768/1024/1440、spacing-scale 4/8、container-width、compact-label-overflow、chip-collection-reflow
6. Typography & Color (MEDIUM): base 16px（12.5px の表は要検討）、line-height 1.5、number-tabular（データ列は等幅数字）、color-semantic（トークン化）、truncation-strategy、long-token-wrapping（パスや UUID は overflow-wrap:anywhere）
7. Animation: state-transition、reduced-motion
8. Forms & Feedback (MEDIUM): input-labels、error-placement（フィールド直下 + aria-describedby）、submit-feedback、required-indicators、empty-states、confirmation-dialogs（削除・全切断・Stop の確認）、input-helper-text、inline-validation、undo-support、success-feedback、error-clarity（原因 + 直し方）、field-grouping（fieldset/legend）、destructive-emphasis、aria-live-errors、error-summary
9. Navigation (HIGH): nav-state-active、deep-linking（タブや詳細を URL で開ける）、state-preservation、adaptive-navigation（≥1024 はサイドバー可）、persistent-nav、destructive-nav-separation
10. Charts & Data (LOW): legend-visible、tooltip-on-interact、axis-labels（単位）、data-table（表の代替）、screen-reader-summary、empty-data-state、loading-chart、number-formatting（locale）、sortable-table（aria-sort）、export-option、time-scale-clarity、contrast-data 3:1、gridline-subtle、color-guidance（赤/緑のみを避ける）

## スキルの UX データベース（ux-guidelines.csv）から得た該当項目
- Table Handling: 表は overflow-x-auto のラッパかカード化。表がレイアウトを壊してはいけない
- Bulk Actions: チェックボックス列 + アクションバー
- Focusable Error Summary: 送信失敗後、summary に focus、各項目からフィールドへリンク、インラインエラーも維持
- Error Messages: aria-live / role=alert
- Submit Feedback: loading → success / error
- Confirmation Dialogs: 削除・不可逆操作の前に確認
- Confirmation Messages: 成功を短く通知（saved toast）
- Focus Appearance: 2px 以上の perimeter、3:1 の state contrast
- Loading Indicators: 待ち時間に見合ったフィードバック。近時の処理で点滅させない
- Content Jumping: 非同期の状態に領域を確保
- Text Reflow: 固定幅・固定高で切らない
- Chip Collection Reflow: 折り返すか +n の開示
- Readable Font Size: モバイル本文 16px
- Sticky Navigation: 固定ナビは本文を隠さない
- Keyboard Navigation: すべての操作をポインタなしで確認
- チャート: 時系列はライン、ライブラリ推奨 Chart.js / ApexCharts / Plotly（CDN 利用時は Vue と同じく vendored fallback が必要）
