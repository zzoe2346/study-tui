# Bundled rendering assets

These assets are distributed independently of the application's license. No
network download is needed while rendering a saved lesson.

| Asset | Upstream | Version / SHA-256 | License |
| --- | --- | --- | --- |
| `vendor/mermaid-11.12.2.min.js` | [Mermaid npm package](https://registry.npmjs.org/mermaid/11.12.2) | 11.12.2 / `d0830a6c05546e9edb8fe20a8f545f3e0dc7c4c3134d584bad9c13a99d7a71e0` | MIT; see `vendor/MERMAID-LICENSE`. The upstream bundle's third-party license notices are preserved at its end. |
| `fonts/NotoSansKR.ttf` | [Google Fonts Noto Sans KR](https://github.com/google/fonts/tree/main/ofl/notosanskr) | Retrieved 2026-10-07 / `194018e6b2b293a7964f037b25c0249ce1418bc9ab3c971060a03aa57861e252` | SIL OFL 1.1; see `fonts/NotoSansKR-OFL.txt` |
| `fonts/NotoSansMono.ttf` | [Google Fonts Noto Sans Mono](https://github.com/google/fonts/tree/main/ofl/notosansmono) | Retrieved 2026-10-07 / `2cb2adb378a8f574213e23df697050b83c54c27df465a2015552740b2769a081` | SIL OFL 1.1; see `fonts/NotoSansMono-OFL.txt` |

The original variable font bytes are retained. Renaming the files does not
change the internal font names. Mermaid and fonts are loaded from this package,
not from a CDN. Mermaid rendering blocks remote requests and disables HTML
labels; lesson SVG is validated before it is used.
