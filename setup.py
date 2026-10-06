from setuptools import setup, find_packages

with open("requirements.txt", "r", encoding="utf-8") as f:
    required = [line.strip() for line in f if line.strip() and not line.startswith("#")]
setup(
    name="AetherBreath",
    version="0.1.0",
    packages=find_packages(),  # 自动发现包
    install_requires=required, #依赖
    python_requires=">=3.10",
)