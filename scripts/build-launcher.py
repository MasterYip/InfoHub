"""Build the portable Windows launcher with .NET Framework's bundled C# compiler."""
import os
import subprocess
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]


def main():
    with Image.open(ROOT / "infohub/apps/web/logo.png") as image:
        image.save(ROOT / "assets/infohub.ico", format="ICO",
                   sizes=[(size, size) for size in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:anycpu",
                    "/optimize+", "/reference:System.Windows.Forms.dll",
                    f"/win32icon:{ROOT / 'assets/infohub.ico'}",
                    f"/out:{ROOT / 'InfoHub.exe'}", str(ROOT / "scripts/InfoHubLauncher.cs")],
                   check=True)


if __name__ == "__main__":
    main()
