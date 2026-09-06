# Semishigure 画面の UI/UX 改修案

作成日: 2026-09-06
対象: `semishigure/ui/static/index.html`（Vue 3、単一ファイル 482 行）と `semishigure/api/app.py`
方法: [ui-ux-pro-max](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill) スキルの設計方針と Quick Reference（優先度 1〜10 の規則）に沿って、4 つの観点（アクセシビリティ・フォーム / 情報設計・作業フロー / ビジュアル・レスポンシブ / チャート・データ）を分担して調査した。根拠はコードの行番号、CSS の色値から計算したコントラスト比、1440 / 768 / 375 px のスクリーンショット（`docs/ui-ux/`）。スキルが返した設計方針は `docs/ui-ux/skill-brief.md`。

## 1. 要約

| 区分 | 内容 |
|---|---|
| 現状の評価 | 機能は揃っているが、画面は「API の状態をそのまま並べた」段階。手順書の流れ（事前チェック → N を段階的に上げる → 記録表に転記）を画面が案内しておらず、危険な操作（全切断・Stop・Burst）に確認が無い。アクセシビリティは `aria-*` / `for` / `:focus-visible` が 0 件。768 px 以下で横スクロールが発生し、1440 px でも左ペインの表がパネルからはみ出す |
| 設計方針 | スキルの分類は Developer Tool / Real-Time Monitor。「ダーク + ミニマル、等幅数字、状態は色と形の両方で、live 表示には更新時刻と stale 状態」を採用。既存のティール（`#0f766e`）は残し、トークン化してライト / ダーク両対応にする |
| 改修の規模 | 3 段階。第 1 段階（小さな修正 12 件、1〜2 日）で安全性とアクセシビリティの穴を塞ぎ、第 2 段階（3〜4 日）で実行画面の再構成とトークン化、第 3 段階（2〜3 日）でチャートと結果画面を作り直す |

## 2. 現状の主な問題（重大度 High）

コントラスト比は WCAG の相対輝度式で計算した値。行番号は `index.html`。

| # | 観点 | 現状 | 根拠 |
|---|---|---|---|
| 1 | 作業フロー | 事前チェックを押すと、サーバが `state.run` を立てるため画面がラン中 UI（スライダー・全切断・Stop run）に 6 秒以上切り替わり、終わると一斉に消えてから結果が出る | `app.py` 263〜282 行、`index.html` 465 行 `runActive` |
| 2 | 安全性 | 全切断・Stop run・Burst に確認ダイアログが無い（削除だけ `confirm()`）。prod プロファイルでも同じ | 196 / 211 / 194 行 |
| 3 | フィードバック | API エラーの表示先が実行タブ内の 1 要素だけ。シナリオ作成の重複名エラー（409）や削除失敗は他のタブでは何も見えない | 213 行 `.err`、346 行 `catch (e) {}` |
| 4 | アクセシビリティ | `label` に `for` が無く、プロファイルフォーム 30 項目・Run フォーム・スライダー・YAML エディタにアクセシブルネームが無い。`aria-*`、`role`、`:focus-visible` は 0 件 | 22 行、111〜121 行、186 行 |
| 5 | レスポンシブ | 768 px で実行 / シナリオ / 結果タブが横スクロール（scrollWidth 1338 / 1339 / 1057）。375 px ではタブが「シナリ／オ」で折れ、YAML 欄が 1 行 3 文字。1440 px でも左ペインの PBX profiles 表の Test ボタンがパネル枠を越える | 19 行 `340px 1fr`、39 行 `nowrap`、`@media` 無し |
| 6 | リアルタイム表示 | WebSocket 切断時に数値とチャートが最後の値のまま残り、更新時刻・stale 表示が無い。「connected」がサーバ接続と PBX 接続の両方に使われている | 296 行、508〜519 行 |
| 7 | 本番環境の表示 | prod は文字色 `#b45309` だけで示され、ヘッダー上のコントラストは 3.53:1。サーバが上限を 20 に切り詰めること（`run.py` 90 行）が画面に出ない | 63 行、`run.py` 86〜90 行 |
| 8 | インタラクション | 非同期中は `opacity:.5` だけで、Start は REGISTER 完了まで数秒〜十数秒無反応に見える。Pause / Burst / ±5 は連打できる。無効時の白文字は 2.16:1 | 29 行、409 行 `post()` |
| 9 | チャート | y 軸の目盛値が `Math.round(maxN/1.1*i/4)` のため「0, 1, 3, 4, 5」と非等間隔で位置と一致しない。凡例と右軸の説明が Canvas 内の固定座標テキストで、375 px では横だけ 0.31 倍に縮んで判読不能。HiDPI 未対応 | 467 / 474 行、49 行 |
| 10 | 記録との対応 | 記録表 xlsx にある指標（INVITE→180、200 max、RTP 5 ms 超件数、486、安定時の channels / %CPU / nlwp、ESL / 切断理由 / ログ件数）の多くが画面に無く、ラン同士を並べて比較できない | `report/xlsx.py` 48〜95 行 |

Medium / Low の項目（入力枠の 1.29:1、`--muted` 4.83:1、ティールと緑の系列色 1.09:1、見出し階層、空状態、文言の日英混在、日時形式の不一致など）は 4 節の一覧に含める。

## 3. 設計方針

### 3.1 方向性

- **分類**: Developer Tool / Real-Time Monitor（スキルの product 検索結果）。推奨は「Dark Mode + Minimalism & Swiss」「Real-Time Monitor + Terminal」「Dark syntax theme + Blue focus」「JetBrains Mono + IBM Plex Sans」。
- **採用**: ダーク前提の配色をトークンとして用意し、`prefers-color-scheme` に追従 + 手動切替。既定はライトのまま（既存の運用と手順書のスクリーンショットを壊さない）。
- **不採用**: スキルの design system が 2 回目の検索で返した Glassmorphism / Cinzel + Josefin Sans（不動産向け）は、運用ツールに合わないため採用しない。スキルのアクセント `#22C55E`（緑）は「確立 / OK」の意味色と衝突するため、アクセントは既存ティール系に固定する。
- **Real-Time / Operations パターンの必須要件**: 「live」と表示するのは裏付けがある時だけ。更新時刻と stale 状態を出す。更新頻度の制御と一時停止を用意する。reduced-motion では静的スナップショット。

### 3.2 デザイントークン（抜粋）

```css
:root {
  color-scheme: light dark;
  --bg:#f4f5f7; --panel:#ffffff; --surface-2:#f1f3f5;
  --text:#1f2933; --muted:#52525b;            /* muted 7.7:1（現状 #6b7280 は 4.8:1） */
  --line:#dfe3e8; --line-strong:#94a3b8;      /* 入力枠は line-strong（現状 1.29:1） */
  --accent:#0f766e; --accent-hover:#0d6b64; --on-accent:#fff; --ring:#2563eb;
  --ok:#15803d; --ok-bg:#dcfce7; --warn:#b45309; --warn-bg:#fef3c7; --bad:#b91c1c; --bad-bg:#fee2e2;
  --prod:#b91c1c; --on-prod:#fff; --info:#1d4ed8;
  --chart-0:#64748b; --chart-1:#0f766e; --chart-2:#b45309; --chart-3:#1d4ed8; --chart-4:#6d28d9;
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:24px; --sp-6:32px;
  --fs-xs:11px; --fs-sm:12px; --fs-tbl:13px; --fs-base:14px; --fs-md:16px; --fs-xl:24px; --fs-2xl:34px;
  --font-sans:"IBM Plex Sans JP","IBM Plex Sans",system-ui,"Hiragino Sans","Noto Sans JP",sans-serif;
  --font-mono:"JetBrains Mono",ui-monospace,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { /* 下と同じ */ } }
:root[data-theme="dark"] {
  --bg:#0f172a; --panel:#1b2336; --surface-2:#272f42; --text:#f8fafc; --muted:#94a3b8;
  --line:#334155; --line-strong:#64748b; --accent:#2dd4bf; --accent-hover:#14b8a6; --on-accent:#0f172a; --ring:#60a5fa;
  --ok:#4ade80; --ok-bg:#14532d; --warn:#fbbf24; --warn-bg:#78350f; --bad:#f87171; --bad-bg:#7f1d1d; --prod:#ef4444; --on-prod:#000;
  --chart-0:#94a3b8; --chart-1:#2dd4bf; --chart-2:#fbbf24; --chart-3:#60a5fa; --chart-4:#a78bfa;
}
```

- チャートの系列色は teal / amber / blue / purple / neutral に割り当て直し（赤と緑の対を作らない）、線種（実線 / 破線 / 点線）を併用する。Canvas 側は `getComputedStyle` でトークンを読む。
- 数値は `font-variant-numeric: tabular-nums`（タイル、表の数値列、ヘッダーの経過秒）。
- フォントは CDN に頼らず `ui/static/vendor/fonts/` に woff2 を同梱（Vue と同じ方式、OFL）。フォールバックは現行のシステムフォント。
- elevation は 3 段（`bg` → `panel` は border → `tile` は `surface-2` の塗り）。影はダイアログとトーストだけ。
- 主 CTA は画面に 1 つ。`danger` の塗りは「ランを停止」だけ、「全通話を切る」「削除」は outline。タブの選択状態はボタンの `primary` を流用せず、`aria-selected` と下線で示す。

### 3.3 コンポーネント規約

ボタン / パネル / タイル / 表 / バッジ / フォームの CSS は `docs/ui-ux/skill-brief.md` と同じ調査で作成した規約案（担当レビューの (c)）に従う。要点: `:focus-visible { outline:2px solid var(--ring); outline-offset:2px }`、`.table-wrap { overflow-x:auto }`、`td.num { text-align:right }`、`.tag` は SVG アイコン + `.sr-only` テキスト、`button:disabled` は opacity ではなく `surface-2` + `muted`。

## 4. 改修項目一覧

重大度: High = 安全性・作業に支障、Medium = 使いにくさ・規則違反、Low = 仕上げ。「規則」はスキルの Quick Reference の項目名。

### 4.1 作業フローと情報設計

| # | 重大度 | 項目 | 改修案 | 規則 |
|---|---|---|---|---|
| F1 | High | 事前チェック中に画面がラン中 UI へ切り替わる | スナップショットに `kind: "precheck"` を付け、画面はフォーム内に「事前チェック中… n/6 秒」の進行表示（領域は常に確保）。Start と事前チェックは disabled + spinner | state-clarity, content-jumping |
| F2 | High | 全切断・Stop run・Burst に確認が無い | `<dialog>` で「対象 host（env）、現在の確立数」を示して確認。prod は赤帯。最低限 `confirm()` を 3 か所に | confirmation-dialogs, destructive-emphasis |
| F3 | High | ラン中パネルに主操作が 8 個並ぶ | 主操作は「目標 N（スライダー・±）」だけ。段階実行・プリセット・レート・一時停止・バーストは折りたたみ。「全通話を切る」「ランを停止」はパネル最下部の危険ゾーンに分離 | primary-action, destructive-nav-separation |
| F4 | High | ラン終了後に結果への導線が無い | 終了時に右列上部へ「ラン #13 が終了（28.2 s）: 結果を見る / 記録表 xlsx / 同じ設定で再実行」のバナー。ヘッダーの状態に finished を明示 | 作業フロー |
| F5 | High | prod の表示が色だけ、上限 20 が見えない | フォーム下に「この設定で実行します」ブロック（host / env / 上限 N / 使う secret 名 / 発信と応答の内線 / プラグイン）。prod は `PROD` バッジ（`#fbbf24` 地に濃色、10.6:1）+ cap 欄を 20 に固定 | color-not-only, input-helper-text |
| F6 | Medium | 段階的に N を上げる指定がラン前にできない | ラン前フォームに「固定 N / 段階（5:180,10:180,20:180）/ プリセット A・B」のラジオ。書式のヘルプ文を可視化 | 作業フロー, input-helper-text |
| F7 | Medium | ラン中の状況把握（ステップ、残り秒、back-off 理由）が散在 | 右列最上部に状態バー: `N=5（確立 5 / 保留 0 / 失敗 0）・経過 21.8 s・ステップ 1/3 残り 168 s → 次 N=10・[一時停止中][自動減少: 486×3]`。back-off の理由は日本語で「PBX が 486 を 3 回連続で返したため目標を 4 に下げました」 | contextual-live-badge-updates |
| F8 | Medium | 実行タブに PBX profiles 表と過去ラン表が重複 | 実行タブから両表を外し、「選択中のプロファイルを Test」ボタンと「最近のラン 5 件」だけにする。一覧は PBX タブ / 結果タブに一本化 | consistency |
| F9 | Medium | 右列の順序（PBX ログの下に自分の通話一覧） | 状態バー → 同時数チャート → 通話（失敗・保留を先頭）→ PBX ホスト（要約タイル、threads / log tail は折りたたみ）→ プラグイン（折りたたみ）。イベントとログは「新しい順」で統一 | 情報設計 |
| F10 | Medium | タブと詳細に URL が無い | `location.hash`（`#run`, `#results/12`）でタブと詳細 ID を同期。`<nav>` + `aria-current="page"` | deep-linking, nav-state-active |
| F11 | Medium | 結果タブの詳細が表の下に出て、どの行かが分からない | 選択行をハイライト（`aria-selected`）し、詳細は右ドロワーか表直下のパネルで「閉じる / 前 / 次」 | state-clarity |
| F12 | Medium | secret の解決が Start まで分からない | 事前チェックに「秘密情報の解決」項目（名前のみ、値は出さない）。F5 のブロックにも `secret: fusion_9100 ✓ / esl ✗` | error-feedback |
| F13 | Low | ラン名を付けられない | フォームに「ラン名 / メモ」（`StartRequest.name` は API に既にある） | 作業フロー |
| F14 | Low | 文言の日英混在（RUN / Start / 全切断 / Pause / Burst to） | 主言語を日本語に統一（開始 / 停止 / 一時停止 / バースト / 発信レート）。英語は API 名として mono 表示に限定。`Burst` `Ramp` の説明を title ではなく可視のヘルプ文に | consistency |

### 4.2 アクセシビリティとフォーム

| # | 重大度 | 項目 | 改修案 | 規則 |
|---|---|---|---|---|
| A1 | High | エラーが実行タブ以外で見えない | `.err` をヘッダー直下の共通バナー（`role="alert"`）へ。`createScenario` の `catch (e) {}` を `editor.msg` に流す。`deleteScenario` / `deleteProfile` が `r.ok` を見ていないので `api()` を通す | error-feedback, aria-live-errors |
| A2 | High | label と入力の関連付けが無い | `v-for` 内は `:for="'pf-'+f"` / `:id`。スライダーに `aria-label` と `aria-valuetext`、Burst 入力・シナリオ select に `aria-label`。placeholder だけの入力に可視ラベル | form-labels |
| A3 | High | 非同期中の状態と連打 | `busy` を文字列にし `aria-busy` + 文言変更（「開始中…」）。`post()` にも pending を持たせ各ボタンを disabled。無効色は `surface-2` + `muted`（4.8:1） | loading-buttons, submit-feedback |
| A4 | High | prod のコントラスト 3.53:1、色のみ | F5 のバッジ化。一覧の env 列も同じ | color-contrast, color-not-only |
| A5 | Medium | 事前チェックの ✔ ✖ が文字グリフ | インライン SVG + `.sr-only` の「OK / NG」。NG 行は背景色も併用。結果ブロックに `role="status"` と「NG の項目: n 件」 | no-emoji-icons, color-not-only |
| A6 | Medium | ライブ領域が無い | 接続状態に `role="status"`、`.err` に `role="alert"`、保存結果に `role="status"`。タイルには付けず、10 秒スロットルの `.sr-only` 要約文（目標 / 確立 / 失敗）を用意 | contextual-live-badge-updates |
| A7 | Medium | プロファイルフォーム 30 項目が平坦 | `fieldset/legend` で 基本 / SIP / RTP / SSH / ESL・監視 / その他 にグループ化。SSH は `executor==='ssh'` の時だけ表示。`environment` は select、ポートは `type="number" inputmode="numeric"`。`_ref` 項目にヘルプ文 + `aria-describedby`。新規時の初期値を dataclass の既定値に合わせる（現状は空文字で上書きされる） | field-grouping, input-helper-text |
| A8 | Medium | 空状態が無い | ランなし / シナリオなし / 事前チェック未実施 / 詳細未選択 に案内文と次の操作。領域は `min-height` で確保 | empty-states, content-jumping |
| A9 | Medium | 入力枠 1.29:1 | `--line-strong:#94a3b8` を入力枠に（ダークは `#64748b`、3.3:1） | non-text contrast |
| A10 | Medium | タブの選択状態が AT に伝わらない | `<nav aria-label="画面切替">` + `aria-current`、または `role="tablist"` + ←→ キー | nav-state-active |
| A11 | Medium | フォーカス・押下・ホバーの状態 | `:focus-visible` 2 px アウトライン（ヘッダー上は明色）、`:active`、`primary` / `danger` にホバー色（現状は `button:hover` に上書きされて変化しない） | focus-states, press-feedback |
| A12 | Medium | チェックボックスが 13 px、名前なし | `label` で包み 24×24 のヒット領域、`.sr-only` に「run 12 を選択」 | web-target-size |
| A13 | Medium | 必須・エラーのフィールド結び付け | `required` / `aria-required`、`aria-invalid` + `aria-describedby` でフィールド直下にエラー。428（prod 未確認）は該当チェックボックスへフォーカス | error-placement |
| A14 | Medium | YAML 検証結果の見せ方 | 失敗文言を textarea 直下のブロック（`role="alert"`、`pre-wrap`）にし textarea へフォーカス。成功は 4 秒で消えるトースト。「解釈結果」は `h3`、左列は `th scope="row"` | inline-validation, success-feedback |
| A15 | Low | 見出し階層が平坦（すべて h2） | タブごとに h2、パネル h3、パネル内小見出し h4 | heading-hierarchy |
| A16 | Low | title 属性だけの説明、同名ボタンの連続 | 説明は可視の `small` + `aria-describedby`。「詳細」「Test」に `aria-label`（run 12 の詳細） | aria-labels |

### 4.3 ビジュアルとレスポンシブ

| # | 重大度 | 項目 | 改修案 | 規則 |
|---|---|---|---|---|
| V1 | High | 横スクロール（768 / 375）と 1440 の表のはみ出し | `@media (max-width:1023px) { main { grid-template-columns:1fr } }`、全表を `.table-wrap { overflow-x:auto }` で包む、`.grid2 > * { min-width:0 }`、`header { flex-wrap:wrap }`、JSON セルは `pre-wrap; overflow-wrap:anywhere` | horizontal-scroll, Table Handling |
| V2 | High | 配色がライト固定、色がハードコード（`#fff` `#111827` `#e5e7eb` など 6 か所 + Canvas 内） | 3.2 のトークンに置換。`color-scheme: light dark`。切替はヘッダーの小さなトグル（システム / ライト / ダーク） | dark-mode-pairing, color-semantic |
| V3 | Medium | 系列色 ティール `#0f766e` と緑 `#15803d` が 1.09:1、結果チャートは赤緑対比 | `--chart-*` に再割当て（target は中立灰の破線、確立は teal、est+pending は amber、PBX channels は blue、RTP late は purple） | color-guidance |
| V4 | Medium | 文字サイズが 11 / 12 / 12.5 / 13 / 14 と混在、`--muted` 4.83:1 | 7 段の階層（11 / 12 / 13 / 14 / 16 / 24 / 34）。表 13 px、タイル見出し 12 px、ログ 12 px。`--muted` を `#52525b` | font-scale, color-contrast |
| V5 | Medium | 見出しの `uppercase` でラン名の大小文字が失われる、絶対パスが見出しで 4 行 | 識別子は `.id`（mono、`text-transform:none`）。パスは見出しから外し `.meta.path`（末尾優先の省略 + title） | truncation-strategy, long-token-wrapping |
| V6 | Medium | タイルのキーがはみ出す（`LOG_PATTERNS.CHANNEL_LIFETIME.COUNT`）、ESL events の値が切れる | key:value の羅列（ESL イベント、切断理由、応答内線、プラグイン metrics）はタイルではなく小型表かバッジ列。`.tile { min-width:0 }` + ellipsis + title | compact-label-overflow |
| V7 | Medium | panel と tile が同じ見た目、タブ選択と主 CTA が同じ塗り | elevation 3 段。タブは独自スタイル。`danger` 塗りは「ランを停止」のみ | elevation-consistent, primary-action |
| V8 | Low | spacing が 6 / 7 / 9 / 10 / 14 / 18 px 混在 | `--sp-1..6`（4 / 8 / 12 / 16 / 24 / 32）に丸める | spacing-scale |
| V9 | Low | 遷移・reduced-motion の定義なし | `transition .15s`（色のみ）+ `@media (prefers-reduced-motion: reduce)` で無効化。ライブ更新は間隔 2 s に落とす | state-transition, reduced-motion |
| V10 | Low | フォント | IBM Plex Sans JP + JetBrains Mono を同梱（woff2、`font-display:swap`）。見出しは 600、本文 400、ラベル 500 | typography |

### 4.4 チャートとデータ

| # | 重大度 | 項目 | 改修案 | 規則 |
|---|---|---|---|---|
| C1 | High | y 目盛の値が位置と一致しない、右軸に目盛が無い | nice ticks（1 / 2 / 5 × 10^n）で刻みを決め、その位置に線と値。左軸 = 本数、右軸 = ms または %。軸端に単位。x 軸は `m:ss` で 5〜8 本 | axis-labels |
| C2 | High | 凡例が Canvas 内、375 px で判読不能、HiDPI でぼやける | 凡例は HTML（色 + 線種 + 名前 + 単位 + 現在値、クリックで系列切替）。`canvas.width = clientWidth × devicePixelRatio` + ResizeObserver。`role="img" aria-label` に要約文 | legend-visible, screen-reader-summary |
| C3 | High | ホバーで値が読めない | 最寄り点のツールチップ（t と全系列の値）。キーボード ←→ で移動 | tooltip-on-interact |
| C4 | High | 記録表の指標が画面に無い、ラン比較ができない | `generic_rows()` を返す `/api/runs/{id}/rows` を追加し、詳細画面に xlsx と同じ「指標 / 値」表を同じ順序で表示。複数選択で「並べて比較」（列 = ラン、チャートは重ね描き） | data-table, export-option |
| C5 | Medium | 監視の取得時刻・stale が無い、プロセス名が `freeswitch` 固定 | 見出しに「最終取得 05:26:17（2 s 間隔・35 samples）」、`now − last.wall > 3 × interval` で stale バッジ + 淡色化。名前は `pbx_profile.type` から | Real-Time パターン |
| C6 | Medium | 閾値超過の強調が 3 か所だけ | タイルに `data-state="ok|warn|bad"`（RTP 5 ms 超件数、486 連続、back-off、ESL エラー、stale）。枠色 + SVG アイコン + 文脈付きの `aria-live` 通知 | state-clarity |
| C7 | Medium | 表: 数値が左寄せ、単位なし、ソートなし、日時がブラウザロケール | `td.num` 右寄せ + tabular-nums、見出しに単位（late max (ms)）、`th aria-sort` でソート、通話一覧に「失敗のみ / 内線」フィルタ、日時は xlsx と同じ `2026-09-06 05:15:51` | sortable-table, number-formatting |
| C8 | Medium | 長時間ランで x 軸の始点が動く（直近 900 点）、結果詳細は全点描画 | 「直近 15 分 / 全体」の表示範囲を明示して切替。サーバ側で min/max バケットに間引く。再描画は 1 s スロットル | time-scale-clarity, debounce-throttle |
| C9 | Medium | 空・読込中・失敗のチャートが真っ白 | 高さを確保したまま「サンプル待ち」「読み込み中…」「samples がありません（未完了のラン）」を重ねる | empty-data-state, loading-chart |
| C10 | Low | ログ tail の見出しが累計行数、レベル色なし。イベントの順序が画面で不統一 | 「直近 80 / 累計 2,500 行 · WARN 0 ERR 0」、行頭にレベル色バー、WARNING 以上フィルタ。イベントは新しい順に統一、kind をタグ表示 | consistency |
| C11 | Low | 更新を止めて読めない | 「更新を一時停止」トグルと間隔（0.5 / 1 / 2 s）。reduced-motion 時は既定 2 s | Real-Time パターン |

チャートの実装は、手描き Canvas の改善（目盛計算・二軸・DPR・ヒットテスト・凡例・キーボード操作を 3 関数分自作）ではなく、**uPlot（MIT、約 45 KB、依存なし、アニメーション無し、多軸とカーソル追従が標準）** を `ui/static/vendor/` に同梱して置き換えることを推奨する。Chart.js は既定でアニメーションがあり、1 万点では decimation が必要になる。

## 5. 改修後の画面構成

### 5.1 共通

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ 蝉時雨 Semishigure  [実行] [シナリオ] [PBX] [結果]      ● サーバ接続中 最終更新 05:26:39 │ ← nav + aria-current、URL #run / #results/12
├──────────────────────────────────────────────────────────────────────────────┤
│ 共通アラート帯（role=alert）: API エラー / 切断中（n 秒前の値）/ prod 警告          │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 5.2 実行タブ（ラン前）

```
┌ 左 360px ──────────────────────────┐ ┌ 右 ────────────────────────────────────────┐
│ 実行                               │ │ この設定で実行します                          │
│ 1. 何を掛けるか                    │ │  PBX 192.0.2.2 pbx.semishigure.test [dev]     │
│  シナリオ [fusionpbx ▾]            │ │  プロファイル fusionpbx-local (local) 監視 ON  │
│  PBX プロファイル [(シナリオ指定)▾]│ │  上限 N 50   ← prod なら「20（prod 固定）」赤帯  │
│ 2. どう上げるか                    │ │  発信 9100 → 8001 / 応答 9001〜9004（各 5）     │
│  (•) 固定 N [1]                    │ │  秘密情報 fusion_9100 ✓ esl ✓                  │
│  ( ) 段階 [5:180,10:180,20:180]    │ ├──────────────────────────────────────────────┤
│  ( ) プリセット [A ▾]              │ │ 事前チェック（最後: 05:20 OK / 未実施）        │
│  発信レート [0.5] 本/秒 通話長 [180]│ │  進行 ●●●○○ 4/6 秒（領域は常に確保）           │
│ 3. 記録  ラン名 / メモ [________]  │ │  [✓] REGISTER 9001 200 in 12 ms                │
│ ▸ 詳細設定                         │ │  [✗] 応答側 RTP 受信 level 0 → 対処…           │
│ ☐ 本番環境に負荷を掛けることを確認 │ ├──────────────────────────────────────────────┤
│ [事前チェック]  [▶ 開始] ← 主 CTA  │ │ 最近のラン 5 件 → 結果タブへ                    │
└────────────────────────────────────┘ └──────────────────────────────────────────────┘
```

### 5.3 実行タブ（ラン中）

```
┌ 左 360px ──────────────────────────┐ ┌ 右 ────────────────────────────────────────┐
│ 実行中 ui-shot 21.8 s [dev]         │ │ 状態バー（aria-live=polite）                   │
│ 目標 N   5 / 上限 50                │ │ N=5 確立 5 / 保留 0 / 失敗 0 · ステップ 1/3     │
│ [━━━●━━━━━━━━━━━]  [−5][−1][+1][+5]│ │ 残り 168 s → 次 N=10 [一時停止中][自動減少 486×3]│
│ ▸ 段階実行 / プリセット             │ ├──────────────────────────────────────────────┤
│ ▸ 発信レート / 通話長               │ │ 指標タイル 6 枚（→200 p50/p95、発信/終了、       │
│ ▸ 一時停止 / バースト               │ │  RTP late max・5 ms 超、rx lost、calls/s、内線）│
│ 応答内線 9001 2/5 9002 1/5 …        │ ├──────────────────────────────────────────────┤
│ イベント（新しい順）                │ │ 同時数チャート（HTML 凡例、二軸、ツールチップ）  │
│─────────────────────────────────── │ ├──────────────────────────────────────────────┤
│ 危険な操作                          │ │ 通話 (5) 失敗・保留を先頭、状態バッジ           │
│ [全通話を切る] [■ ランを停止]       │ ├──────────────────────────────────────────────┤
│ （どちらも確認ダイアログ）          │ │ PBX ホスト fusionpbx-local ● 最終取得 05:26:17 │
└────────────────────────────────────┘ │  channels 10 / %CPU 4 / threads 35 / ERR 0     │
                                       │  ▸ threads / log tail（折りたたみ）             │
                                       │ ▸ プラグイン log_patterns（折りたたみ）         │
                                       └──────────────────────────────────────────────┘
```

### 5.4 実行タブ（ラン後）

右列上部に「✓ ラン #13 ui-shot が終了しました（28.2 s） [結果を見る] [記録表 xlsx] [同じ設定で再実行]」。その下に要約タイルと同時数チャート（生データ表とログは畳む）。左列はラン前フォームに戻り、前回の値を保持する。

### 5.5 結果タブ

行クリックで `#results/<id>`、選択行をハイライト。詳細は表の右（1440 以上）または表直下の閉じられるパネル（前 / 次 / 閉じる）。詳細には xlsx と同じ「指標 / 値」表。2 件以上選択で「並べて比較」と「xlsx 出力（n 件）」のアクションバー。

### 5.6 レスポンシブ

| 幅 | main | 表 | ヘッダー |
|---|---|---|---|
| ≥ 1024 | `340px minmax(0,1fr)` | 全列 | 1 行 |
| 768〜1023 | 1 列。操作パネル → 状態バー → チャート → 表 | `.table-wrap` で横スクロール、id / name 列は sticky | 1 行 |
| ≤ 767 | 1 列、`padding 8px` | 過去ランは id / name / failed / p95 だけ表示 | 折り返し（h1 + 状態 / ラン情報 / タブ 4 個を等幅）。入力は 16 px |

## 6. 実施計画

| 段階 | 内容 | 項目 | 目安 |
|---|---|---|---|
| 1. 安全性とアクセシビリティ | 確認ダイアログ、共通エラー帯、label 関連付け、busy 状態、prod バッジ、事前チェックの進行表示、横スクロール、フォーカス表示、SVG アイコン、stale 表示、見出し階層、空状態 | F1 F2 F5 A1〜A6 A9〜A12 A15 V1 C5 | 1〜2 日 |
| 2. 構造とトークン | 実行画面の 3 状態と危険ゾーン、段階実行の事前指定、状態バー、重複表の除去、右列の順序、URL 同期、トークン化とダーク対応、文字階層、フォント同梱、プロファイルフォームのグループ化、文言の統一 | F3 F4 F6〜F11 F13 F14 A7 A8 A13 A14 V2〜V10 | 3〜4 日 |
| 3. チャートと結果 | uPlot 導入（凡例・二軸・ツールチップ・DPR・間引き・空状態）、系列色、`/api/runs/{id}/rows`、詳細の指標表、ラン比較、表のソートと単位、日時形式、ログ tail の改善、更新一時停止 | C1〜C4 C6〜C11 V3 | 2〜3 日 |

第 1 段階は CSS と小さな Vue 変更だけで、API は事前チェックのスナップショット種別（`kind`）の追加のみ。第 2 段階以降は `index.html` を分割（`app.js`、`styles.css`、コンポーネント数個）することを勧める。現状 482 行の単一ファイルは、状態バーやドロワーを足すと追いにくくなる。テストは既存の `tests/test_api.py` に加え、Playwright で 375 / 768 / 1440 の横スクロール検査（`scrollWidth <= innerWidth`）と、axe-core によるアクセシビリティ検査を CI に足す。

## 7. 判断をお願いしたい点

| 項目 | 提案 | 理由 |
|---|---|---|
| 既定テーマ | システム設定に追従し、手動切替を用意。切替が無いときはライト | スキルはダーク前提を推奨するが、手順書のスクリーンショットと既存の運用はライト。トークン化すれば両方を同じ品質で維持できる |
| フォント | IBM Plex Sans JP + JetBrains Mono を同梱（OFL） | CDN が使えない環境がある（Vue と同じ理由）。同梱しない場合は現行のシステムフォントのまま階層だけ整理する |
| チャートライブラリ | uPlot（MIT）を vendored で追加 | 手描き Canvas の改善は 3 関数分の作り込みになり、凡例・ツールチップ・二軸・DPR を自作することになる |
| 画面の主言語 | 日本語に統一（API 名は mono で英語） | 現状は RUN / Start / 全切断 / Pause が混在。手順書と記録表は日本語 |
| URL | `location.hash` によるタブと詳細の同期 | サーバ側のルーティング変更なしで deep link ができる。将来 SPA ルータに移る場合も置き換えやすい |
| ファイル分割 | 第 2 段階で `index.html` を `styles.css` / `app.js` / コンポーネントに分割 | 現状の 482 行単一ファイルに状態バー・ドロワー・ダイアログを足すと保守しにくい。ビルド工程は入れない（ES modules をそのまま配信） |

## 8. 参考

- スキルの設計方針と規則の抜粋: `docs/ui-ux/skill-brief.md`
- スクリーンショット（現状）: `docs/ui-ux/run_1440.png`（ラン前）、`running_1440.png`（ラン中）、`results_1440.png`（結果）、`run_375.png`（375 px の横スクロール）、`scenarios_768.png`（YAML 欄の潰れ）
