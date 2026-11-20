"""评估 worker 的 AgentCore Runtime 代码包入口 (打包时作为 zip 根目录的 main.py)。"""
from evaluation.worker import app

if __name__ == "__main__":
    app.run()
