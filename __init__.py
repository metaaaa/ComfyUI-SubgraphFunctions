"""ComfyUI-SubgraphFunctions

UI で保存したサブグラフを「関数」として扱う。定義をワークフローに埋め込まず、
user/default/subgraphs/functions/ のファイルを実行時に展開して呼ぶので、
大本を直すとそれを使っている全ワークフローの挙動が次の実行から変わる。

詳細は README.md / README.ja.md と subgraph_functions.py の冒頭。
"""

from .subgraph_functions import (
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
)

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
