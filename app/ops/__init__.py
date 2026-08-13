"""运维面轻量工具：热路径结构化日志等。不宣称 M5.3 production_logging_claim。"""

from app.ops.hot_path_log import emit_hot_path_log, hot_path_logging_enabled

__all__ = ["emit_hot_path_log", "hot_path_logging_enabled"]
