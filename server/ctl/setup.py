from setuptools import setup

setup(
    name="cerebro-ctl",
    version="0.1.0",
    py_modules=["cerebro_ctl"],
    install_requires=["httpx>=0.27.0", "typer>=0.12.0"],
    entry_points={"console_scripts": ["cerebro-ctl=cerebro_ctl:app"]},
)
