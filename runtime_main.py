"""AgentCore Runtime 代码包入口 (打包时作为 zip 根目录的 main.py)。

Runtime 以 `opentelemetry-instrument main.py` 启动, 由 ADOT 自动导出 trace。
"""
from agent.main import app

if __name__ == "__main__":
    app.run()
