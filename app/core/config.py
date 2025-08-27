from dataclasses import dataclass
from pathlib import Path


@dataclass
class AppConfig:
    host: str = '0.0.0.0'
    port: int = 8699
    option_file: str = './option.yml'
    pdf_dir: str = './pdf'

    @property
    def option_path(self) -> Path:
        return Path(self.option_file).resolve()

    @property
    def pdf_path(self) -> Path:
        return Path(self.pdf_dir).resolve()


config = AppConfig()
