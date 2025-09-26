# 한 번만 실행
from pathlib import Path
import base64

def encrypt_text(plain: str) -> str:
    return base64.b64encode(plain.encode("utf-8")).decode("utf-8")

def make_encrypted_files():
    plugin_dir = Path(__file__).parent
    for src, dst in [
        ("sample_responses.txt", "sample_responses_encrypted.txt"),
        ("processing_tools.txt", "processing_tools_encrypted.txt")
    ]:
        text = Path(plugin_dir / src).read_text(encoding="utf-8")
        enc = encrypt_text(text)
        Path(plugin_dir / dst).write_text(enc, encoding="utf-8")
        print(f"{dst} created.")

make_encrypted_files()