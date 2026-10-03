# rtk-sf 利用マニュアル

**バージョン 0.11.0** · [English](manual.md)

rtk-sf は、Salesforce プロジェクトを事前にインデックス化し、その圧縮ビューを
Claude Code に渡す MCP サーバーです。1,800 行の Apex クラスを読ませる代わりに、
150 トークンの構造サマリーを読ませます。

この削減は、一度きりの節約よりも大きな意味を持ちます。Claude Code は会話を
キャッシュしますが、キャッシュされたトークンも**以降のすべてのターンで再読み込み
されます**。そのため、早い段階でコンテキストに入った内容は、おおよそ
`1.25 +（残りターン数 × 0.1）`倍のコストになります。実際の 242 リクエストの
セッションで計測した値は **23.5 倍**でした。ツールの出力を小さく保つことが、
rtk-sf の設計思想そのものです。

---

## 1. 動作要件

| | |
|---|---|
| Python | 3.9 以上 |
| Claude Code | 最近のバージョンであれば可 |
| Salesforce CLI (`sf`) | `sf_command` と `soql_query` を使う場合のみ |
| Ollama | ローカル委譲（第 6 章）を使う場合のみ。完全に任意 |

---

## 2. インストール

rtk-sf は **PyPI では公開していません**。git から直接インストールしてください。

```bash
pip install "rtk-sf[all] @ git+https://github.com/furuCRM-Inc/rtk-sf.git@v0.11.0"
```

`pip install rtk-sf` は 404 エラーで失敗します。

任意の追加機能が不要な場合は、次のように絞ってインストールできます。

```bash
# Salesforce のみ（ベクトル検索・OCR なし）
pip install "rtk-sf @ git+https://github.com/furuCRM-Inc/rtk-sf.git@v0.11.0"

# ベクトル再ランキングを追加（numpy）
pip install "rtk-sf[vector] @ git+https://github.com/furuCRM-Inc/rtk-sf.git@v0.11.0"
```

確認:

```bash
python3 -m rtk_sf --version     # 0.11.0
```

### アップグレード

```bash
pip install --upgrade --force-reinstall --no-deps \
  "rtk-sf[all] @ git+https://github.com/furuCRM-Inc/rtk-sf.git@v0.11.0"
```

`--force-reinstall` は必須です。これを付けないと pip は git インストールを
再解決せず、「Requirement already satisfied」と表示して何もしません。
`--no-deps` は、リリースノートに依存関係の追加が明記されていない限り安全です。

アップグレード後は再インデックスしてください。リリース間で spec の形式が
変わることがあります。

---

## 3. プロジェクトの初期設定

Salesforce プロジェクトのルートで、2 つのコマンドを実行します。

```bash
cd /path/to/your-salesforce-project

# 1. インデックスを作成（.rtk-sf/ が作られます）
python3 -m rtk_sf index

# 2. Claude Code に登録
claude mcp add rtk-sf -- python3 -m rtk_sf serve
```

`npx rtk-sf` ではなく `python3 -m rtk_sf` を使ってください。rtk-sf は Python
パッケージであり、npm パッケージではありません。

登録の確認:

```bash
claude mcp list        # rtk-sf が表示されること
```

その後 Claude Code のセッションで「search_codebase で CSV 取込のロジックを
探して」のように依頼します。ファイルの中身ではなくコンポーネント名で answer が
返ってくれば、正しく動作しています。

### 作成されるファイル

```
your-project/
└── .rtk-sf/
    ├── db.sqlite        # FTS5 検索インデックス
    ├── specs/           # コンポーネントごとの圧縮 YAML spec（自動生成）
    ├── relations.json   # 依存関係グラフ
    ├── registry.json    # 差分インデックス用の mtime 記録
    └── handover/        # ローカル委譲の状態（第 6 章を使う場合のみ）
```

`.rtk-sf/` は `.gitignore` に追加してください。コマンド 1 つで再生成できる
派生物です。

`specs/` は**自動生成され、上書きされます**。直接編集しないでください。
再インデックス後も残したい情報は `annotate_component` で記録します。

---

## 4. 日常の使い方

これらのツールを利用者が直接呼ぶことはありません。呼ぶのは Claude です。
ツール名を知っておく価値は、Claude が生ファイルを読もうとしたときに
「まず `get_relations` を使って」と指示できる点にあります。

### コードを把握する

| ツール | 用途 |
|---|---|
| `search_codebase` | 名前やキーワードからコンポーネントを探す。まずここから |
| `query_compressed_spec` | 1 コンポーネントの項目・メソッド・概要を読む |
| `get_class_skeleton` | Apex クラスを部分的に読む。指定メソッドだけ本文を展開し、他は折りたたむ |
| `get_relations` | 修正前の影響範囲調査。呼び出し元と呼び出し先 |
| `list_components` | 種別ごとの一覧（全 Apex クラス、全オブジェクト、全フロー） |
| `annotate_component` | 判明した業務ルールを記録し、次のセッションの起点にする |

### データとスキーマ

| ツール | 用途 |
|---|---|
| `nl_to_soql` | 日本語または英語のまま、データに関する質問を投げる。手書き SOQL の**前に**試す |
| `soql_query` | 行数を自動で切り詰めつつ SOQL を実行 |
| `get_object_schema` | テストデータ作成用の項目一覧 |
| `get_record_types` | XML を読まずにレコードタイプ定義を取得 |
| `get_lwc_targets` | 公開されている LWC コンポーネントと、その公開先 |
| `read_data_file` | CSV / JSON / JSONL の構造をプレビュー |

`nl_to_soql` は決定論的なコンパイラで、内部でモデルを呼びません。
`{"intent": "UNKNOWN"}` が返った場合のみ、`get_object_schema` と手書きの
`soql_query` に切り替えてください。`RECORD_UPDATE` の結果は**提案**であり、
確認なしに DML として実行されることはありません。

### 検証とデプロイ

| ツール | 用途 |
|---|---|
| `validate_apex` | ローカル事前チェック。ループ内 SOQL/DML、括弧の不整合、デバッグ文の残り |
| `validate_soql` | 組織に投げる前のクエリのローカル事前チェック |
| `sf_command` | `deploy` / `validate` / `retrieve` / `run_test` / `describe`。CLI の生出力ではなく要約を返す |

`validate_apex` は無料で、組織との往復も発生しません。
`sf_command(action="deploy")` の前に実行してください。

### 他言語のサポート

同じスケルトンの考え方を、エンタープライズ構成の他の言語にも適用します。

| 言語 | ツール |
|---|---|
| Java | `get_java_skeleton` / `run_java_build`（Maven・Gradle） |
| Kotlin | `get_kotlin_skeleton` / `run_gradle` |
| TypeScript / JS | `get_ts_skeleton` / `run_js_tests`（Jest・Vitest・Playwright） |
| Python | `get_python_skeleton` / `run_python_tests`（pytest） |

`run_*` 系は、成功時は成功件数の 1 行だけ、失敗時は失敗したケースのみを
返します。全件グリーンのテストに 4,000 トークンを払う必要はありません。

### ドキュメントと運用

| ツール | 用途 |
|---|---|
| `export_system_documentation` | システム仕様書・シーケンス図・ユースケースを**ファイルとして**生成 |
| `get_project_timeline` | 「最近何をしていたか」。集約済みの作業履歴 |
| `get_roi_stats` | このセッションで削減したトークンと金額 |
| `extract_image_text` | スクリーンショットのローカル OCR（日英対応） |
| `compact_prompt` | 長い日英混在プロンプトから冗長な部分を除去 |

`export_system_documentation` は結果を返さずディスクに書き出します。
チャット内のシーケンス図は以降の全ターンでコストがかかりますが、
ディスク上のファイルはゼロです。

---

## 5. インデックスの更新

インデクサーはファイルの mtime を追跡するため、再インデックスは差分のみで
軽量です。

```bash
python3 -m rtk_sf index                      # 変更されたファイルのみ
python3 -m rtk_sf index --force              # 全件再構築
python3 -m rtk_sf index --path force-app/x   # サブディレクトリのみ
```

作業中に自動更新したい場合:

```bash
python3 -m rtk_sf watch                      # --path も指定可能
```

`sf project retrieve` の後、ブランチを切り替えた後、rtk-sf をアップグレードした
後は再インデックスしてください。

検索結果が空のときは、インデックスが古いか存在しないことがほとんどです。
まず `python3 -m rtk_sf index` を実行してください。

---

## 5b. コマンド一覧

| コマンド | 内容 |
|---|---|
| `index` | インデックスの作成・更新。`--force`、`--path DIR` |
| `watch` | ファイル変更を監視して自動で再インデックス。`--path DIR` |
| `serve` | MCP stdio サーバーの起動（Claude Code が実行するのはこれ） |
| `ui` | アーキテクチャ図の生成。`--output FILE`（既定 `dist/architecture_map.html`） |
| `docs` | システムドキュメントをディスクに出力。種別または `all`、`--output-dir DIR` |
| `timeline` | 作業履歴を JSON で出力。`last_7_days` などのスコープを指定 |
| `hook-stats` | OCR / NLP フックの最近の動作を表示。`-n N` |
| `install` | 一括セットアップ（インデックス作成・`CLAUDE.md` の書き換え・フック設定）— **注意事項あり** |
| `update` | rtk-sf を更新し、`install` を再実行 — **注意事項あり** |

グローバルオプション `--project-root DIR` はすべてのコマンドで使用でき、
プロジェクト外から操作できます。

> **`install` と `CLAUDE.md` の関係**
> `install` は `<!-- rtk-sf:begin <バージョン> -->` で囲まれた rtk-sf 用の
> ガイダンスブロックを更新します。0.11.0 以降、この再実行は安全です。
> ツール表はインストール済みパッケージが実際に提供するツールから生成され、
> ブロックはその場で置き換わるため利用者が書いたセクションの位置は変わりません。
> 変更前のファイルは `CLAUDE.md.rtk-bak` として保存され、より**新しい**
> rtk-sf が書いたブロックは上書きされません。
> ただしブロック**内部**に書いた内容は置き換わるため、独自のメモは
> 別のセクションに記述してください。
>
> `.claude/settings.json` のフックは、`python3` で `rtk_sf` を
> インポートできる場合は移植性のある `python3` を使用し、できない場合のみ
> 絶対パスを使用します。そのため、コミットした `settings.json` は
> チーム内で共有しても動作します。
>
> `CLAUDE.md` を完全に自分で管理したい場合は、第 3 章の 2 ステップの
> セットアップの方が変更範囲が小さく済みます。

---

## 6. ローカル委譲（任意・0.11.0 の新機能）

Apex の作業をメソッド単位で、ローカルのコードモデルと Claude に振り分けます。
定型的な修正はローカルで実行し、影響範囲の大きいものは Claude が担当します。
**既定では無効**です。何も起動していない状態でも rtk-sf は完全に動作します。

### 準備

```bash
# コードモデルを取得
ollama pull qwen2.5-coder:7b

# 任意: 既定以外のホスト、またはファインチューンを指定
export RTK_SF_WORKER_URL=http://mac-mini.local:11434
export RTK_SF_WORKER_MODEL=qwen2.5-coder-7b-nexusmesh
```

ファインチューンを使う場合は `RTK_SF_WORKER_MODEL` の指定が**必須**です。
自動検出は既知のコードモデル名（`qwen2.5-coder`、`deepseek-coder`、
`codellama` など）で判定するため、独自のタグ名はどれにも一致せず、
適切なモデルであっても「コード特化モデルなし」として拒否されます。

### 3 つのツール

| ツール | 動作 |
|---|---|
| `hybrid_plan` | 全メソッドを採点し、振り分け先を提示する。最初にこれを呼ぶ |
| `hybrid_delegate` | メソッド単位の修正をまとめてローカルモデルで実行する |
| `hybrid_review` | レビュー指摘を記録する、または引き継ぎ差分を読む |

`hybrid_plan` の出力例:

```
plan CsvImportController (CsvImportController.cls, 7 methods)
worker: local:qwen2.5-coder:7b
local tiers: LOW only

-> LOCAL WORKER (1):
  computeHeaderMatchScore [LOW] score=5 loc=9
-> CLAUDE (6):
  importChunk [HIGH] score=24 loc=36 blockers=partial-dml flags=remote-entry
  RowResult [HIGH] score=3 loc=5 NOT-DELEGATABLE:constructor (no return type)
  ...
```

### 必ず Claude が担当するもの

採点はソースコードから導出され、ファイル内のラベルには依存しません。
ラベルはコードが変わった瞬間に古くなるためです。次の**ブロッカー**に該当する
メソッドは、どれほど短くても Claude に回されます。

ループ内の SOQL / DML ・ `Savepoint` / `rollback` ・ 部分 DML
（`Database.insert(...)`）・ HTTP コールアウト ・ `without sharing` ・
FLS / CRUD チェック ・ バッチ Apex ・ `Messaging.send`

コンストラクタは対象外です。`@AuraEnabled` はスコアを上げますが、
ブロックはしません。

`MEDIUM` 相当のメソッドも、`allow_medium` を渡さない限り Claude が担当します。

### 生成コードを読まずに信頼できる理由

次のすべてを満たさない限り、`.cls` ファイルには 1 バイトも書き込まれません。

1. 出力が途中で切れていない
2. 依頼したメソッド名そのものとして、ちょうど 1 つのメソッドに構文解析できる
3. 波括弧・丸括弧・文字列リテラルの対応が取れている
4. **ファイル内の他のすべてのメソッド本体がバイト単位で同一**である
   （追加・削除・変更がない）
5. `validate_apex` の ERROR レベルのルールに該当しない

4 番目が、コードを読まずに済ませる根拠です。クラス全体を書き換えて 3 つの
メソッドを失わせるような挙動を、これが検出します。変更前のファイルは
`<name>.cls.rtk-bak` として保存され（バッチごとに 1 回なので、常にバッチ前の
状態に戻せます）、書き込み自体はアトミックです。

**これは構造的な保証であり、意味的な保証ではありません。** 1 つのメソッドのみを
変更し、ファイルを壊していないことは証明できますが、ロジックが正しいかどうかは
判断できません。それはテストで確認してください。

### 自己修正の仕組み

出力が差し戻されると、その判定理由が修正履歴として保存され、次の試行で
モデルに渡されます。Claude のトークンは消費しません。
構造チェックでは検出できない意味的な問題は `hybrid_review` で指摘します。

```
hybrid_review(component_name="AccountService",
              method="validateBillingAddress",
              critique="acc が null の場合、acc.BillingPostalCode の参照で
                        例外になります。先頭に null チェックを追加してください。")
```

直近 2 件の指摘は、失敗したメソッド本体とともにそのまま保持されます。
そのメソッドが通過した時点で、指摘はコードを含まない短いプロジェクト
ルールに要約され、以降は**すべてのメソッド**に適用されます。これにより、
1 つのメソッドで得た教訓が、そのメソッドの修正とともに失われることがありません。

### 対応範囲

Apex のみです。ローコード・ノーコードのメタデータ（フロー、入力規則、
項目定義）は**対象外**です。検証の過程で、入力規則の修正時に無関係な
`<errorMessage>` が黙って書き換えられる事象が発生し、それを検出した XML 用の
チェック機構はまだ本番品質に達していません。

---

## 7. トラブルシューティング

**`rtk-sf: command not found`**
`python3 -m rtk_sf ...` を使ってください。コンソールスクリプトが PATH に
入っていない可能性があります。

**検索結果が空になる**
再インデックスしてください（`python3 -m rtk_sf index`）。0 件と表示される場合は、
プロジェクトルートにいるか、その配下に `.cls` ファイルが存在するかを確認します。

**Claude Code に MCP サーバーが表示されない**
```bash
claude mcp list
claude mcp remove rtk-sf
claude mcp add rtk-sf -- python3 -m rtk_sf serve
```
仮想環境にインストールした場合は、Python の絶対パスを指定してください。

**サーバーがすぐ終了する**
ターミナルで直接実行してエラーを確認します（`python3 -m rtk_sf serve`）。
ログは stderr に出力され、stdin を待ち受けるため、手動起動時に「何も出ない」のは
正常な状態です。

**`hybrid_*` が `worker: none` を返す**
モデルを起動していない場合の正常な動作です。すべて Claude に振り分けられ、
ファイルは一切書き込まれません。起動済みの場合は `RTK_SF_WORKER_URL` を確認し、
タグがファインチューンであれば `RTK_SF_WORKER_MODEL` を指定してください。
接続失敗の結果は 30 秒間キャッシュされるため、起動直後は少し待って再試行します。

**付けた注記が消えた**
`annotate_component` で記録した内容は消えません。ただし
`.rtk-sf/specs/` に直接書き込んだ内容は、自動生成の際に失われます。

---

## 8. rtk-sf が行わないこと

- **リンターやコンパイラではありません。** `validate_apex` は正規表現による
  事前チェックで、ガバナ制限や構文の典型的な誤りを検出します。組織に対する
  `sf_command(action="validate")` の代替にはなりません。
- **動作の正しさは判断しません。** 第 6 章のすべての保証は構造的なものです。
  コードが正しいかを教えてくれるのは、依然としてテストだけです。
- **自発的にモデルを呼び出しません。** rtk-sf が行う唯一の外部通信は第 6 章の
  ローカルワーカーへの接続であり、それも利用者が有効化した場合のみです。
- **確認なしに DML を実行しません。** `nl_to_soql` の `RECORD_UPDATE` は
  提案にとどまります。

---

## 関連ドキュメント

- [`installation.md`](installation.md) — OS ごとのインストール手順（macOS / Linux / Windows・WSL2）
- [`mcp-integration.md`](mcp-integration.md) — MCP プロトコルの詳細、Cline 設定、JSON-RPC デバッグ
- [`hybrid-orchestration.md`](hybrid-orchestration.md) — 第 6 章の設計根拠
- [`documentation-engine.md`](documentation-engine.md) — `export_system_documentation` の詳細
- [`roi.md`](roi.md) — トークン削減のモデル
- [`CHANGELOG.md`](../CHANGELOG.md) — リリースごとの変更点
