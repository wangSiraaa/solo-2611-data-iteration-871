"""受限 YAML 配置目录服务与配置消费分析仓。

模块划分（至少两个实质模块）：

* ``yaml_parser`` / ``materialize`` / ``model`` / ``codec``
  —— 受限 YAML 解析、锚点别名与 merge 语义、来源证据、持久化编解码；
* ``catalog`` —— 命名配置产品目录（可独立用于人工检查）；
* ``csv_pipeline`` / ``ingest`` —— 消费仓任务、CSV 读取与按配置归一化。
"""

__all__ = ["__version__"]
__version__ = "1.0.0"
