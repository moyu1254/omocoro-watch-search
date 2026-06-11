# Deploy

This static site is ready for GitHub Pages.

1. Create or choose a public GitHub repository.
2. Push this workspace to the repository's `main` branch.
3. In the repository settings, enable Pages with **Source: GitHub Actions**.
4. Run **Deploy search site** from the Actions tab, or push a change under `outputs/omocoro-watch-search/`.

The public URL will be:

```text
https://<owner>.github.io/<repository>/
```

The weekly index update workflow runs at Sunday 00:30 JST and commits updated search data. That commit triggers the Pages deploy workflow.
