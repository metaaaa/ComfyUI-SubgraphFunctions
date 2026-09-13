# ComfyUI-SubgraphFunctions

保存したサブグラフを**関数として共有する**。大本を 1 箇所直せば、それを使って
いる全ワークフローの挙動が次の実行から変わる。ワークフローの保存し直しも
ComfyUI の再起動も要らない。

[English](./README.md)

## 何が問題か

ComfyUI のサブグラフは**値渡し**でコピーされる。ブループリントを挿すと、その
定義の実体がワークフローのファイルに埋め込まれる:

```
my_workflow.json
   definitions.subgraphs[0]  id=4c314f31-…  nodes=15   ← 定義の実体がここに埋まる
   nodes[#105].type = "4c314f31-…"                     ← インスタンスは UUID を指すだけ
```

`app/subgraph_manager.py` はブループリントのファイルを配るだけで、参照を保持せず
バージョン照合もしない。だから「大本」を直しても、既に使っているワークフローには
届かない。10 本のワークフローが同じローダー構成を共有していたら、10 回直すことになる。

## このパックがすること

定義をワークフローに**埋め込まない**。ノードを 1 個置くだけにして、実行時に
ディスク上のファイルからグラフを組み立てる。

```
user/default/subgraphs/functions/<name>.json   ← UI の publish / edit がそのまま書く
      │  /object_info のたびに走査し直す
      ▼
SubgraphFn_<name>  ノード 1 個                   ← ワークフローにはこれだけ置く
      │  実行のたびにファイルを読み直す
      ▼
ComfyUI のノード展開 ("expand")                   ← 本物のグラフが実行時に生える
```

サブグラフごとに Python を書く必要はない。`.json` を置けばノードが生え、
消せばノードも消える。

## 導入

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/metaaaa/ComfyUI-SubgraphFunctions
```

一度だけ ComfyUI を再起動する。ComfyUI 本体以外の依存はない。

## 使い方

### 関数を作る

普通にサブグラフを組んで選択し、**publish** する。ただし名前欄に
**`functions/<名前>`** と打つこと。これでブループリントが
`user/default/subgraphs/functions/` に落ちる = このパックが走査する場所に入る。

> 走査するのは `subgraphs/functions/` だけで、`subgraphs/` 直下は見ない。
> 分けないと、publish したブループリントが**すべて**黙ってノードになってしまう。

ブラウザを再読込すると、**Subgraph Functions** カテゴリに `fn: <名前>` が出る。

何も作らずに試すなら、同梱の例を置く:

```bash
cp examples/subgraphs/add.json <ComfyUI>/user/default/subgraphs/functions/
```

### 呼ぶ

`fn: <名前>` ノードを置く。入出力はサブグラフの入出力スロットから決まり、
各ウィジェットは**中で繋がっている先の定義を継承**する。COMBO の選択肢、
INT の min/max/step、multiline、tooltip がそのまま効く。

### 編集する

左サイドバーの**ノードライブラリ** → `Subgraph Blueprints/User` → 項目に
カーソルを合わせて出る**鉛筆ボタン**。ブループリントが編集用に開くので、
普通に保存する。`fn: <名前>` を使っている全ワークフローが次の実行から変わる。

ファイルを直接書き換えても同じ。`IS_CHANGED` がファイルのハッシュを見ているので、
外側のワークフローが同一でも古いキャッシュは返らない。

### 同じ名前が 2 つ出る

ノード検索に同じサブグラフが 2 回出る。意味が逆なので見分けること:

| 検索結果 | カテゴリ | 挿すと |
|---|---|---|
| `<名前>` | `Subgraph Blueprints/User` | 定義が**コピーされる**(ComfyUI 標準の挙動) |
| **`fn: <名前>`** | `Subgraph Functions` | ファイルを**参照する**(このパック) |

### 反映に必要な操作

| 変更 | 必要な操作 |
|---|---|
| 中身だけ | **なし**(次の実行から効く) |
| 入出力の増減 | **ブラウザ再読込** |
| ファイルの追加 / 削除 | **ブラウザ再読込** |

`/object_info` が返される直前にディレクトリを走査し直す middleware を入れてある
(名前 + mtime + サイズが変わったときだけ登録し直す)。ComfyUI の再起動は要らない。
mtime を変えずに中身を差し替えた場合だけ `GET /subgraph_functions/reload` で強制できる。

## 仕組み

ノードは `{"expand": <API 形式のグラフ>, "result": (...)}` を返す。ComfyUI の
executor がそのグラフを実行中の prompt に継ぎ足す
([`execution.py`](https://github.com/comfyanonymous/ComfyUI/blob/master/execution.py)
の `expand` 分岐)。保存されている UI 形式のサブグラフを API 形式に直す作業は、
`widgets_values` を各ノードの `INPUT_TYPES` に突き合わせて名前に戻し、litegraph の
スロット番号からリンクを組み直すこと。

知っておくと良い帰結が 2 つ:

- **展開されたノードは 1 個ずつキャッシュされる。** 関数の中のウィジェットを
  1 つ変えても、再実行されるのはそこから下流のノードだけで、関数全体ではない
- **関数の中の出力ノードは動く。** サブグラフ内に置いた `SaveImage` /
  `SaveVideo` はちゃんと検出されて実行される

リンク型の入力 (MODEL / IMAGE など) は `rawLink` で宣言してあるので、解決済みの
値ではなく**リンクそのもの**が展開後のグラフに差し込まれる。上流のキャッシュが
効いたままになる。

## 制限

いずれも黙って誤動作させず、明確なエラーにしてある:

- 関数の中の**入れ子サブグラフ**は未対応
- **mute (mode 2) / bypass (mode 4)** されたノードは展開時に再現できない
- **繋がっていないサブグラフ出力**
- **キャンバスで関数の中身を開けない。** 埋め込まないことの代償なので、
  編集は上記のブループリント経由で行う
- 展開されたノードは `validate_inputs` を通らない (バリデーションは元の prompt に
  対してのみ走る)。設定ミスは**実行時エラー**として出る
- 同じ basename のファイルを `subgraphs/` と `subgraphs/functions/` の両方に置くと、
  ComfyUI 自身のブループリント一覧で衝突する (frontend は basename をキーにする)

## 書き残しておく罠

frontend は `control_after_generate` を持つウィジェット (代表例は seed) の
**直後に 1 枠余計に** `widgets_values` を挿す。`widgets_values` を位置で読む
コードは同じだけ読み飛ばさないと、以降の値が 1 つずつずれる。エラーにならず、
値だけが静かに変わる。`subgraph_functions.py` の `has_control_widget` がこれを見ている。

## ライセンス

MIT
