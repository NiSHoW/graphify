# Compila graphify in un singolo graphify.exe standalone (Nuitka onefile).
# Prerequisiti: uv; un compilatore C (se manca, Nuitka scarica MinGW da solo
# grazie a --assume-yes-for-downloads). Build tipica: 10-30 minuti.
# Uso:  pwsh scripts/build-exe.ps1 [-Extras "neo4j,mcp"]
param(
    # Extra opzionali da includere nell'exe (vuoto = solo il core AST).
    [string]$Extras = "neo4j,mcp"
)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
Set-Location $repo

# Launcher minimale: l'entry point e' lo stesso dello script console `graphify`.
$launcher = Join-Path $env:TEMP "graphify_launcher.py"
@'
from graphify.__main__ import main
if __name__ == "__main__":
    main()
'@ | Set-Content $launcher

# Installa gli extra richiesti nel venv di build, cosi' Nuitka li puo' seguire.
if ($Extras) {
    $Extras.Split(",") | ForEach-Object { uv pip install ".[$($_.Trim())]" | Out-Null }
}

# I tree-sitter-* e gli extra sono importati dinamicamente: Nuitka non li vede
# dal solo grafo degli import, quindi vanno inclusi esplicitamente.
$dynamicPkgs = uv run python -c @'
import importlib.metadata as m
names = set()
for d in m.distributions():
    n = (d.metadata["Name"] or "").replace("-", "_")
    if n.startswith("tree_sitter"):
        names.add(n)
for extra in ("neo4j", "mcp", "starlette"):
    try:
        m.distribution(extra); names.add(extra)
    except m.PackageNotFoundError:
        pass
print("\n".join(sorted(names)))
'@
$includeFlags = $dynamicPkgs -split "`n" | Where-Object { $_ } | ForEach-Object { "--include-package=$_" }

uv run python -m nuitka `
    --onefile --standalone --assume-yes-for-downloads `
    --output-dir=dist --output-filename=graphify.exe `
    --include-package=graphify `
    --include-package-data=graphify `
    --include-package=networkx --include-package=numpy --include-package=rapidfuzz `
    @includeFlags `
    --company-name="Graphify Labs" --product-name="graphify" `
    --file-description="Turn any folder of code and docs into a queryable knowledge graph" `
    $launcher

Write-Host "`nFatto: dist\graphify.exe" -ForegroundColor Green
Write-Host 'Prova rapida:  dist\graphify.exe --help ; dist\graphify.exe update .'
