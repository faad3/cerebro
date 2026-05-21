from setuptools import setup, find_packages

setup(
    name="cerebro-node",
    version="0.2.0",
    description="Cerebro node daemon — connects a host to a Cerebro master "
    "and serves PTY-backed Claude Code agents",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "websockets>=12.0",
        "typer>=0.12.0",
        "httpx>=0.27.0",
    ],
    entry_points={
        "console_scripts": [
            "cerebro-node=cerebro_node.cli:main",
            "cerebro=cerebro_node.cli:main",
        ],
    },
)
