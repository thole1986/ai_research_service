import shutil
import subprocess
from pathlib import Path


def main():
    subprocess.run(["uv", "sync", "--extra", "dev", "--frozen"], check=True)
    if not Path(".env").exists():
        shutil.copyfile(".env.example", ".env")
    executable = Path(".venv/Scripts/alembic.exe")
    if not executable.exists():
        executable = Path(".venv/bin/alembic")
    subprocess.run([str(executable), "upgrade", "head"], check=True)
    print("Ready. Configure .env locally, then start API, both workers and Beat.")


if __name__ == "__main__":
    main()
