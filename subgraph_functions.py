"""UI で保存したサブグラフ (SubgraphBlueprint) を、実行時に展開して呼ぶ。

ComfyUI のサブグラフは定義の実体がワークフローごとにコピーされる
(app/subgraph_manager.py は定義ファイルを配るだけで、参照は保持しない)。
だから大本を直しても、既にそれを使っているワークフローには伝播しない。

ここでは「定義をワークフローに埋め込まない」方向で解決する:

  user/default/subgraphs/functions/<name>.json   ← UI の publish / edit が書く
        |  (/object_info のたびに走査 = ブラウザ再読込で追随)
        v
  SubgraphFn_<name> ノード 1 個           ← ワークフローにはこれだけ置く
        |  (実行のたびにファイルを読み直す)
        v
  ComfyUI のノード展開 (execution.py の "expand")

ファイルを直せば、そのノードを置いた全ワークフローの挙動が次の実行から変わる。
ComfyUI の再起動もワークフローの再生成も要らない。

代償は、キャンバス上で中身を開いて見られないこと。中身を触りたいときは
ノードライブラリの "Subgraph Blueprints/User" から本体を開いて編集する。

widgets_values の読み方は ComfyUI の UI 形式 -> API 形式への変換 (litegraph の並び順を解く)。
control_after_generate を持つウィジェットの直後に frontend が 1 枠余計に
挿す件は、ここでも同じだけ読み飛ばさないと以降の値が 1 つずつずれる
(エラーにならず値だけ静かに変わるので、一番たちが悪い)。

v1 で対応していないもの (いずれも黙って誤動作させず例外にする):
  - 入れ子のサブグラフ
  - mute (mode 2) / bypass (mode 4)
  - 繋がっていないサブグラフ出力
"""

import hashlib
import json
import logging
import os

import folder_paths
import nodes as comfy_nodes
from comfy_execution.graph_utils import GraphBuilder

# litegraph がサブグラフの入出力ノードに使う固定 id
INPUT_NODE_ID = -10
OUTPUT_NODE_ID = -20

NODE_PREFIX = "SubgraphFn_"
CATEGORY = "Subgraph Functions"
# subgraphs/ のうち、関数として扱うものだけを入れるサブディレクトリ
FUNCTIONS_SUBDIR = "functions"

# ウィジェットで値を入れる型。それ以外はリンクで受ける
# (ComfyUI 本体が COMBO / primitives をウィジェットとして扱うのに合わせる)
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}

# グラフには載るが実行されないノード
SKIP_TYPES = {"Note", "MarkdownNote"}

log = logging.getLogger(__name__)


def subgraphs_dir():
    """関数として運用するサブグラフだけを置く場所。

    frontend の SubgraphBlueprint.basePath = 'subgraphs/' 配下なら、UI は
    サブディレクトリでも拾う (/userdata を recurse=true で呼んでいる)。
    そこで 1 階層切って、通常運用のブループリントと混ぜない。

      user/default/subgraphs/              普通のブループリント (コピーされる)
      user/default/subgraphs/functions/    ここだけ SubgraphFn_* として登録する

    切らないと、UI で publish したブループリントが**すべて**ノードになってしまう。
    UI から新しく置くときは publish の名前欄に "functions/<名前>" と打つ
    (userdata が親ディレクトリを作るので、それだけでここに入る)。
    """
    return os.path.join(folder_paths.get_user_directory(), "default",
                        "subgraphs", FUNCTIONS_SUBDIR)


def is_widget_type(t):
    if isinstance(t, list):          # 明示的な選択肢リスト
        return True
    if not isinstance(t, str):
        return False
    return t in WIDGET_TYPES or "COMBO" in t


def has_control_widget(info):
    """seed のように control_after_generate を持つウィジェットか。"""
    opts = info[1] if len(info) > 1 and isinstance(info[1], dict) else {}
    return bool(opts.get("control_after_generate"))


def schema_inputs(class_type):
    """[(name, info, is_widget)] を input_order の順で返す。

    /object_info を叩く必要はない。ノードクラスから直接読める。
    """
    cls = comfy_nodes.NODE_CLASS_MAPPINGS.get(class_type)
    if cls is None:
        raise ValueError(f"未登録のノード型です: {class_type}")
    spec = cls.INPUT_TYPES()
    out = []
    for section in ("required", "optional"):
        for name, info in (spec.get(section) or {}).items():
            if not isinstance(info, (list, tuple)) or not info:
                continue
            out.append((name, info, is_widget_type(info[0])))
    return out


def class_type_of(node):
    """litegraph のノードから API 形式の class_type を取る。"""
    return (node.get("properties") or {}).get("Node name for S&R") or node.get("type")


def iter_links(sg):
    """サブグラフのリンクを {origin_id, origin_slot, target_id, target_slot} で返す。

    定義内のリンクは dict 形式だが、トップレベルのワークフローと同じ配列形式
    [id, origin_id, origin_slot, target_id, target_slot, type] で書かれている
    ものも読めるようにしておく。
    """
    for link in sg.get("links") or []:
        if isinstance(link, dict):
            yield (link["origin_id"], link["origin_slot"],
                   link["target_id"], link["target_slot"])
        elif isinstance(link, (list, tuple)) and len(link) >= 5:
            yield (link[1], link[2], link[3], link[4])


# --------------------------------------------------------------------------
# ブループリントの読み込み
# --------------------------------------------------------------------------

def load_blueprint(path):
    """(workflow, subgraph) を返す。

    publish されたファイルは「インスタンスノード 1 個 + definitions.subgraphs」
    という形に frontend 側でバリデーションされている (subgraphStore の
    validateSubgraph)。念のため root ノードから引き直す。
    """
    with open(path, "r", encoding="utf-8") as f:
        wf = json.load(f)
    subgraphs = (wf.get("definitions") or {}).get("subgraphs") or []
    if not subgraphs:
        raise ValueError(f"サブグラフ定義がありません: {path}")
    root = (wf.get("nodes") or [None])[0]
    sg = None
    if root is not None:
        sg = next((s for s in subgraphs if s.get("id") == root.get("type")), None)
    return wf, sg or subgraphs[0]


def file_signature(path):
    """IS_CHANGED 用。中身が変わったら実行し直させる。

    これが無いと、外側のワークフローが同一なら ComfyUI がキャッシュを返し、
    ブループリントを直しても結果が変わらない。
    """
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError as e:
        return f"missing:{e}"


def _inner_widget_spec(sg, slot):
    """サブグラフ入力 slot が繋がっている先の、本来のウィジェット定義を返す。

    こうすると COMBO の選択肢・INT の min/max・multiline がそのまま効く。
    見つからなければ None。
    """
    nodes_by_id = {n.get("id"): n for n in sg.get("nodes") or []}
    for oid, oslot, tid, tslot in iter_links(sg):
        if oid != INPUT_NODE_ID or oslot != slot:
            continue
        node = nodes_by_id.get(tid)
        if node is None:
            continue
        entries = node.get("inputs") or []
        if tslot >= len(entries):
            continue
        entry = entries[tslot]
        name = (entry.get("widget") or {}).get("name") or entry.get("name")
        try:
            for n, info, _ in schema_inputs(class_type_of(node)):
                if n == name:
                    return info
        except ValueError:
            return None
    return None


def declare(wf, sg):
    """サブグラフの入出力から (input_spec, return_types, return_names, order) を作る。

    ウィジェット既定値はインスタンスノードの widgets_values から取る。
    inputs のうち「リンク型を除いたもの」が widgets_values と同じ順に並ぶ
    (公式テンプレートの実データで確認済み)。
    """
    root = (wf.get("nodes") or [None])[0] or {}
    wvals = list(root.get("widgets_values") or [])

    required, order = {}, []
    widget_index = 0
    for slot, spec in enumerate(sg.get("inputs") or []):
        name = spec.get("name")
        t = spec.get("type") or "*"
        order.append(name)
        if is_widget_type(t):
            default = wvals[widget_index] if widget_index < len(wvals) else None
            widget_index += 1
            inner = _inner_widget_spec(sg, slot)
            if inner is not None:
                opts = dict(inner[1]) if len(inner) > 1 and isinstance(inner[1], dict) else {}
                if default is not None:
                    opts["default"] = default
                # control_after_generate はこのノードでは意味が無い
                # (展開先の seed ウィジェットは毎回ここから渡される)
                opts.pop("control_after_generate", None)
                required[name] = (inner[0], opts)
            else:
                required[name] = (t, {"default": default} if default is not None else {})
        else:
            # リンク型は解決済みの値ではなくリンクのまま受け取る。
            # 展開したグラフにそのまま差し込めるので、上流のキャッシュが効く
            required[name] = (t, {"rawLink": True})

    outs = sg.get("outputs") or []
    return_types = tuple(o.get("type") or "*" for o in outs)
    return_names = tuple(o.get("name") or o.get("type") or "out" for o in outs)
    return required, return_types, return_names, order


# --------------------------------------------------------------------------
# 実行時展開 (UI 形式 -> API 形式)
# --------------------------------------------------------------------------

def expand(sg, values, order):
    """サブグラフ定義を API 形式のグラフに変換して {"expand":..., "result":...} を返す。

    values: {入力名: 実際の値 or リンク [node_id, slot]}
    """
    incoming, outgoing = {}, {}
    for oid, oslot, tid, tslot in iter_links(sg):
        if tid == OUTPUT_NODE_ID:
            outgoing[tslot] = (oid, oslot)
        else:
            incoming[(tid, tslot)] = (oid, oslot)

    subgraph_ids = {s.get("id") for s in (sg.get("definitions") or {}).get("subgraphs") or []}

    builder = GraphBuilder()
    made = {}
    for node in sg.get("nodes") or []:
        ct = class_type_of(node)
        if ct in SKIP_TYPES:
            continue
        if node.get("mode") in (2, 4):
            raise ValueError(
                f"mute / bypass されたノードが含まれています (id={node.get('id')} "
                f"type={ct})。展開時には再現できないので、ブループリント側で消すか"
                f"有効にしてください")
        if ct in subgraph_ids or ct not in comfy_nodes.NODE_CLASS_MAPPINGS:
            raise ValueError(
                f"展開できないノードです (id={node.get('id')} type={ct})。"
                f"入れ子のサブグラフは v1 では未対応")
        made[node["id"]] = builder.node(ct, id=str(node["id"]))

    def resolve(src):
        origin_id, origin_slot = src
        if origin_id == INPUT_NODE_ID:
            if origin_slot >= len(order):
                raise ValueError(f"サブグラフ入力 {origin_slot} が定義と食い違います")
            return values.get(order[origin_slot])
        node = made.get(origin_id)
        if node is None:
            raise ValueError(f"リンク元のノードが見つかりません (id={origin_id})")
        return node.out(origin_slot)

    for node in sg.get("nodes") or []:
        if node["id"] not in made:
            continue
        target = made[node["id"]]
        ct = class_type_of(node)

        # 1) ウィジェット値を順番に読む。control_after_generate の分を飛ばす
        wvals = list(node.get("widgets_values") or [])
        index = 0
        for name, info, is_widget in schema_inputs(ct):
            if not is_widget:
                continue
            if index < len(wvals):
                target.set_input(name, wvals[index])
            index += 1
            if has_control_widget(info):
                index += 1

        # 2) リンクで上書きする。ウィジェットから変換された入力もここに来る
        for slot, entry in enumerate(node.get("inputs") or []):
            src = incoming.get((node["id"], slot))
            if src is None:
                continue
            name = (entry.get("widget") or {}).get("name") or entry.get("name")
            target.set_input(name, resolve(src))

    results = []
    for slot in range(len(sg.get("outputs") or [])):
        src = outgoing.get(slot)
        if src is None:
            name = (sg["outputs"][slot] or {}).get("name", slot)
            raise ValueError(f"サブグラフ出力 '{name}' が何にも繋がっていません")
        results.append(resolve(src))

    return {"expand": builder.finalize(), "result": tuple(results)}


# --------------------------------------------------------------------------
# ノードの登録
# --------------------------------------------------------------------------

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}


def _make_class(name, path):
    wf, sg = load_blueprint(path)
    required, return_types, return_names, order = declare(wf, sg)

    used = {class_type_of(n) for n in sg.get("nodes") or []}
    is_output = any(
        getattr(comfy_nodes.NODE_CLASS_MAPPINGS.get(ct), "OUTPUT_NODE", False)
        for ct in used if ct in comfy_nodes.NODE_CLASS_MAPPINGS)

    class SubgraphFn:
        BLUEPRINT_NAME = name
        BLUEPRINT_PATH = path
        CATEGORY = globals()["CATEGORY"]
        FUNCTION = "call"
        RETURN_TYPES = return_types
        RETURN_NAMES = return_names
        OUTPUT_NODE = is_output
        DESCRIPTION = (
            f"user/default/subgraphs/functions/{name}.json を実行時に展開する。"
            f"ファイルを直せば、このノードを使う全ワークフローに効く。")

        @classmethod
        def INPUT_TYPES(cls):
            # /object_info のたびに呼ばれる。出力 (RETURN_TYPES) はクラス属性
            # なのでここでは変えられないが、middleware 側がファイルの変化を見て
            # クラスごと作り直すため、入出力どちらもブラウザ再読込で追随する
            try:
                w, s = load_blueprint(cls.BLUEPRINT_PATH)
                spec, _, _, _ = declare(w, s)
            except Exception as e:
                log.warning("blueprint %s の読み込みに失敗: %s", cls.BLUEPRINT_NAME, e)
                spec = required
            return {"required": spec}

        @classmethod
        def IS_CHANGED(cls, **kwargs):
            return file_signature(cls.BLUEPRINT_PATH)

        def call(self, **kwargs):
            w, s = load_blueprint(self.BLUEPRINT_PATH)
            _, _, _, live_order = declare(w, s)
            return expand(s, kwargs, live_order)

    SubgraphFn.__name__ = NODE_PREFIX + name
    return SubgraphFn


def register_all():
    """user/default/subgraphs/*.json を走査してノードとして登録し直す。"""
    directory = subgraphs_dir()
    found, errors = {}, []
    if os.path.isdir(directory):
        for entry in sorted(os.listdir(directory)):
            if not entry.endswith(".json"):
                continue
            name = entry[:-len(".json")]
            path = os.path.join(directory, entry)
            try:
                found[NODE_PREFIX + name] = (_make_class(name, path), name)
            except Exception as e:
                errors.append(f"{entry}: {e}")
                log.warning("blueprint %s を登録できません: %s", entry, e)

    # 前回登録したものを一度落としてから入れ直す (消されたファイルを残さない)
    for key in list(NODE_CLASS_MAPPINGS):
        NODE_CLASS_MAPPINGS.pop(key, None)
        NODE_DISPLAY_NAME_MAPPINGS.pop(key, None)
        comfy_nodes.NODE_CLASS_MAPPINGS.pop(key, None)
        comfy_nodes.NODE_DISPLAY_NAME_MAPPINGS.pop(key, None)

    for key, (cls, name) in found.items():
        NODE_CLASS_MAPPINGS[key] = cls
        NODE_DISPLAY_NAME_MAPPINGS[key] = f"fn: {name}"
        # 起動後の再読込でも効くように本体のマッピングへも直接入れる
        comfy_nodes.NODE_CLASS_MAPPINGS[key] = cls
        comfy_nodes.NODE_DISPLAY_NAME_MAPPINGS[key] = f"fn: {name}"

    return sorted(found), errors


_last_signature = None


def dir_signature():
    """functions/ の中身が変わったかを見るための軽い署名 (名前 + mtime + サイズ)。"""
    directory = subgraphs_dir()
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except OSError:
        return ()
    out = []
    for name in names:
        try:
            st = os.stat(os.path.join(directory, name))
        except OSError:
            continue
        out.append((name, st.st_mtime_ns, st.st_size))
    return tuple(out)


def sync_registry():
    """ディレクトリが変わっていたら登録し直す。変わっていなければ何もしない。"""
    global _last_signature
    signature = dir_signature()
    if signature == _last_signature:
        return False
    _last_signature = signature
    register_all()
    return True


def _add_routes():
    """/subgraph_functions/reload で登録し直せるようにする。

    入力の増減はブラウザ再読込だけで追えるが、出力の増減は RETURN_TYPES が
    クラス属性なのでここを叩く必要がある (ComfyUI の再起動は不要)。
    """
    try:
        from server import PromptServer
    except Exception:
        return
    instance = getattr(PromptServer, "instance", None)
    if instance is None:
        return
    from aiohttp import web

    @instance.routes.get("/subgraph_functions/reload")
    async def _reload(request):
        names, errors = register_all()
        return web.json_response({"registered": names, "errors": errors})

    app = getattr(instance, "app", None)
    if app is None:
        return

    # import 時点では後続のカスタムノードがまだ登録されていない。
    # サーバ起動時にもう一度走らせて、他パックのノードを使うブループリントの
    # ウィジェット定義と OUTPUT_NODE 判定を正しく取り直す
    async def _rescan(_app):
        register_all()

    app.on_startup.append(_rescan)

    # /object_info が来る直前に走査し直す = ブラウザの再読込だけで
    # ファイルの追加・削除が反映される。
    #
    # ハンドラ側は NODE_CLASS_MAPPINGS を回すので、その最中に足すと
    # RuntimeError になる。middleware でハンドラより前に済ませる。
    # PromptServer.__init__ は init_extra_nodes より先に app を作るので、
    # ここ (custom node の import 時) はまだ middlewares を足せる。
    @web.middleware
    async def _rescan_middleware(request, handler):
        if "object_info" in request.path:
            try:
                sync_registry()
            except Exception as e:       # 走査の失敗で /object_info を落とさない
                log.warning("subgraph の再走査に失敗: %s", e)
        return await handler(request)

    try:
        app.middlewares.append(_rescan_middleware)
    except Exception as e:
        # 起動後に読み込まれた等で freeze 済みなら、reload ルートだけ使う
        log.warning("middleware を追加できません (%s)。"
                    "ファイルの追加後は /subgraph_functions/reload を叩くこと", e)


_last_signature = dir_signature()
_names, _errors = register_all()
if _names:
    log.info("SubgraphFunctions: %d 件のサブグラフを登録 (%s)",
             len(_names), ", ".join(_names))
_add_routes()
