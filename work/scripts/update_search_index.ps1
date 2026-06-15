param(
  [string]$PythonExe = "C:\Users\moyuk\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
  [switch]$WithComments,
  [int]$CommentsPerVideo = 20,
  [string]$SiteUrl = "https://omowatch.com/"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$BuildScript = Join-Path $RepoRoot "work\scripts\build_index.py"
$IndexPath = Join-Path $RepoRoot "outputs\omocoro-watch-search\data\search-index.json"

$argsList = @($BuildScript, "--all", "--site-url", $SiteUrl)
if ($WithComments) {
  $argsList += @("--comments-per-video", $CommentsPerVideo)
}

& $PythonExe @argsList
if ($LASTEXITCODE -ne 0) {
  throw "build_index.py failed with exit code $LASTEXITCODE"
}

& $PythonExe -c "import json, pathlib; p=pathlib.Path(r'$IndexPath'); d=json.loads(p.read_text(encoding='utf-8')); assert d.get('videos'), 'No videos found'; print('Updated', len(d['videos']), 'videos')"
if ($LASTEXITCODE -ne 0) {
  throw "search-index validation failed"
}
