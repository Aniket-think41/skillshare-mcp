"""Top-level PyInstaller entry shim.

Same fix as the CLI: PyInstaller must NOT target a module *inside* the package
(skillshare_mcp/server.py) — running that file as __main__ strips its package
context and can break imports / miss dynamically-loaded submodules. This shim
lives outside the package and imports it the normal absolute way, so PyInstaller
bundles the whole `skillshare_mcp` package. Build with:
    pyinstaller --onefile --name skillshare-mcp --collect-all mcp \
      --collect-submodules skillshare_mcp --collect-submodules skillshare_cli run.py
"""

from skillshare_mcp.server import main

if __name__ == "__main__":
    main()
