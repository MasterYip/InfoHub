# Browser Dependencies

Vendored from the exact npm versions in the repository lockfile:

- Marked 18.1.3: `node_modules/marked/lib/marked.umd.js`
- KaTeX 0.19.1: `node_modules/katex/dist/katex.min.js`

Copy these files and their licenses here after upgrading the pinned dependencies.
The application renders Marked tokens as DOM nodes; raw HTML is displayed as text.
KaTeX emits native MathML with untrusted commands disabled, so no CDN, CSS, or
font downloads are needed. The anonymous viewer embeds these scripts into its
self-contained HTML. MiaoDaily copies them into its static deployment.
